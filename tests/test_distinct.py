import pytest

from hare.contrib import test
from hare.exceptions import FieldError, QueryError
from hare.query.expressions import Exists, F, OuterReference, Subquery
from hare.query.functions import Count
from tests.testmodels import (
    Author,
    Book,
    CompositePkThing,
    DefaultOrdered,
    Event,
    IntFields,
    SourceFieldPk,
    Team,
    Tournament,
)

# ---------------------------------------------------------------------------
# Basic DISTINCT (all databases)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_no_args(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    tournaments = await Tournament.objects.all().distinct()
    assert len(tournaments) == 2


# ---------------------------------------------------------------------------
# Plain DISTINCT combined with ORDER BY across a relation - Postgres requires every ORDER BY
# expression to appear in the SELECT list for a plain SELECT DISTINCT (SQLSTATE 42P10); SQLite
# has no such restriction. Both dialects must succeed and agree on the row set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_with_order_by_forward_relation(db):
    author = await Author.objects.create(name="An Author")
    book_1 = await Book.objects.create(name="Book A", author=author, rating=1.0)
    book_2 = await Book.objects.create(name="Book B", author=author, rating=2.0)

    books = await Book.objects.filter().distinct().order_by("author__name")
    assert {book.pk for book in books} == {book_1.pk, book_2.pk}


@pytest.mark.asyncio
async def test_distinct_removes_duplicates_from_m2m_fanout_with_order_by(db):
    """A filter across a many-to-many relation (participants) joins through the through-table,
    fanning a single Event row out into one row per matching participant - .distinct() must
    collapse that fan-out back to one row per Event even while .order_by() is applied against a
    DIFFERENT, forward (single-valued per Event) relation (tournament) - appending that order-by
    column to the SELECT list (this fix) must not reintroduce the fan-out's duplicates, since
    every one of a given Event's fanned-out rows shares the exact same tournament."""
    tournament_1 = await Tournament.objects.create(name="T1")
    tournament_2 = await Tournament.objects.create(name="T2")
    team_a = await Team.objects.create(name="Team A")
    team_b = await Team.objects.create(name="Team B")
    event_1 = await Event.objects.create(name="E1", tournament=tournament_1)
    await event_1.participants.add(team_a, team_b)
    event_2 = await Event.objects.create(name="E2", tournament=tournament_2)
    await event_2.participants.add(team_a)

    non_distinct = await Event.objects.filter(participants__name__isnull=False).order_by("tournament__name")
    assert len(non_distinct) == 3  # fanned out: event_1 once per participant, event_2 once

    distinct_events = (
        await Event.objects.filter(participants__name__isnull=False).distinct().order_by("tournament__name")
    )
    assert [event.name for event in distinct_events] == ["E1", "E2"]


# ---------------------------------------------------------------------------
# Plain DISTINCT + ORDER BY a field the caller did NOT also request via .values()/.values_list()
# (both databases) - resolve_distinct()'s own _include_orderbys_in_select() (base.py) injects
# that ORDER BY column into SELECT to satisfy Postgres's "for SELECT DISTINCT, ORDER BY
# expressions must appear in select list" rule (SQLSTATE 42P10). ValuesQuery/ValuesListQuery
# restrict SELECT to exactly the caller-chosen columns, so silently keeping that injected column
# selected would make DISTINCT dedup on it too, instead of only on what the caller actually asked
# for - see FieldSelectQuery._distinct_needs_python_dedup()'s own docstring for the full story
# and the fix (SQL-level DISTINCT is dropped in favor of a Python-side dedup pass).
# ---------------------------------------------------------------------------


async def _create_tournaments_for_order_by_dedup() -> None:
    # Ordered ascending by `desc`: (C, "v"), (B, "w"), (B, "x"), (A, "y"), (A, "z") - so the
    # first occurrence of each `name`, in ORDER BY order, is C, B, A.
    await Tournament.objects.create(name="A", desc="z")
    await Tournament.objects.create(name="A", desc="y")
    await Tournament.objects.create(name="B", desc="x")
    await Tournament.objects.create(name="B", desc="w")
    await Tournament.objects.create(name="C", desc="v")


@pytest.mark.asyncio
async def test_distinct_values_list_order_by_field_not_selected(db):
    """Live-confirmed bug: without the fix, DISTINCT was silently computed over
    (name, desc) instead of just `name`, so no two rows collapsed (every `desc` is unique) and
    all 5 rows came back instead of the 3 distinct names."""
    await _create_tournaments_for_order_by_dedup()

    names = await Tournament.objects.filter().order_by("desc").distinct().values_list("name", flat=True)
    assert names == ["C", "B", "A"]


@pytest.mark.asyncio
async def test_distinct_values_list_order_by_field_not_selected_respects_limit_offset(db):
    """LIMIT/OFFSET must apply AFTER the Python-side dedup pass, not before - see
    FieldSelectQuery._apply_limit_offset_group_by_and_lock()'s own comment."""
    await _create_tournaments_for_order_by_dedup()

    names = await Tournament.objects.filter().order_by("desc").distinct().limit(2).values_list("name", flat=True)
    assert names == ["C", "B"]

    names = await Tournament.objects.filter().order_by("desc").distinct().offset(1).values_list("name", flat=True)
    assert names == ["B", "A"]


@pytest.mark.asyncio
async def test_distinct_values_order_by_field_not_selected(db):
    """Before the fix this dedupped correctly on SQLite (the extra ORDER BY column was stripped
    back out of SELECT after DISTINCT already ran over it) but raised
    `for SELECT DISTINCT, ORDER BY expressions must appear in select list` (SQLSTATE 42P10) on
    PostgreSQL, since that same strip-back-out left ORDER BY referencing a column no longer in
    SELECT - see test_distinct_values_order_by_field_not_selected_on_postgres below for the
    dialect-specific regression check."""
    await _create_tournaments_for_order_by_dedup()

    rows = await Tournament.objects.filter().order_by("desc").distinct().values("name")
    assert rows == [{"name": "C"}, {"name": "B"}, {"name": "A"}]


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_distinct_values_order_by_field_not_selected_on_postgres(db):
    """Dialect-specific regression check for the SQLSTATE 42P10 half of the bug - live-confirmed
    to raise `hare.exceptions.OperationalError: for SELECT DISTINCT, ORDER BY expressions must
    appear in select list` before the fix; must now both succeed and dedup correctly."""
    await _create_tournaments_for_order_by_dedup()

    rows = await Tournament.objects.filter().order_by("desc").distinct().values("name")
    assert rows == [{"name": "C"}, {"name": "B"}, {"name": "A"}]


# ---------------------------------------------------------------------------
# DISTINCT ON (PostgreSQL only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_on_single_field(db):
    tournament_1 = await Tournament.objects.create(name="1", desc="1")
    await Tournament.objects.create(name="1", desc="2")
    await Tournament.objects.create(name="1", desc="3")

    tournaments = await Tournament.objects.all().distinct("name")
    assert tournaments == [tournament_1]


@pytest.mark.asyncio
async def test_distinct_on_single_field_with_order_by(db):
    await Tournament.objects.create(name="1", desc="1")
    await Tournament.objects.create(name="1", desc="2")
    tournament_3 = await Tournament.objects.create(name="1", desc="3")

    tournaments = await Tournament.objects.all().distinct("name").order_by("name", "-desc")
    assert tournaments == [tournament_3]


@pytest.mark.asyncio
async def test_distinct_on_multiple_fields(db):
    tournament_1 = await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="a")
    tournament_3 = await Tournament.objects.create(name="2", desc="b")

    tournaments = await Tournament.objects.all().distinct("name", "desc").order_by("name", "desc")
    assert tournaments == [tournament_1, tournament_3]


@pytest.mark.asyncio
async def test_distinct_on_values_list_single_field(db):
    """values_list selects one field, same as DISTINCT ON field."""
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")

    tournaments = await Tournament.objects.all().distinct("name").values_list("name", flat=True)
    assert tournaments == ["1", "2"]


@pytest.mark.asyncio
async def test_distinct_on_values_list_multiple_fields(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")

    tournaments = await Tournament.objects.all().distinct("name").values_list("name", "desc")
    assert tournaments == [("1", "a"), ("2", "c")]


@pytest.mark.asyncio
async def test_distinct_on_values_list_extra_fields(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")

    tournaments = await Tournament.objects.all().distinct("name").values_list("desc", flat=True)
    assert tournaments == ["a", "c"]


@pytest.mark.asyncio
async def test_distinct_on_values_list_extra_field_respects_order_by(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")

    tournaments = (
        await Tournament.objects.all().distinct("name").order_by("name", "-desc").values_list("desc", flat=True)
    )
    assert tournaments == ["b", "c"]


@pytest.mark.asyncio
async def test_distinct_on_values_single_field(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")

    tournaments = await Tournament.objects.all().distinct("name").values("name")
    assert tournaments == [{"name": "1"}, {"name": "2"}]


@pytest.mark.asyncio
async def test_distinct_on_values_multiple_fields(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")

    tournaments = await Tournament.objects.all().distinct("name").values("name", "desc")
    assert tournaments == [{"name": "1", "desc": "a"}, {"name": "2", "desc": "c"}]


@pytest.mark.asyncio
async def test_distinct_on_values_extra_fields(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")

    tournaments = await Tournament.objects.all().distinct("name").values("desc")
    assert tournaments == [{"desc": "a"}, {"desc": "c"}]


@pytest.mark.asyncio
async def test_distinct_on_values_extra_field_respects_order_by(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")

    tournaments = await Tournament.objects.all().distinct("name").order_by("name", "-desc").values("desc")
    assert tournaments == [{"desc": "b"}, {"desc": "c"}]


@pytest.mark.asyncio
async def test_distinct_on_only_same_field(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")

    tournaments = await Tournament.objects.all().distinct("name").only("name")
    assert [t.name for t in tournaments] == ["1", "2"]


@pytest.mark.asyncio
async def test_distinct_on_only_extra_field(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")

    tournaments = await Tournament.objects.all().distinct("name").only("name", "desc")
    assert [(t.name, t.desc) for t in tournaments] == [("1", "a"), ("2", "c")]


@pytest.mark.asyncio
async def test_distinct_on_only_with_order_by(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")

    tournaments = await Tournament.objects.all().distinct("name").order_by("name", "-desc").only("name", "desc")
    assert [(t.name, t.desc) for t in tournaments] == [("1", "b"), ("2", "c")]


@pytest.mark.asyncio
async def test_distinct_on_filter_by_model(db):
    tournament_1 = await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    tournament_2 = await Tournament.objects.create(name="2", desc="c")
    tournaments = await Tournament.objects.filter(name__in=["1", "2"]).distinct("name")
    assert [tournament_1, tournament_2] == tournaments


@pytest.mark.asyncio
async def test_distinct_on_source_field(db):
    await SourceFieldPk.objects.create(name="1")
    await SourceFieldPk.objects.create(name="2")
    assert sorted(row.name for row in await SourceFieldPk.objects.all().distinct("id")) == ["1", "2"]


@pytest.mark.asyncio
async def test_distinct_on_annotate_by_model(db):
    await Tournament.objects.create(name="1", desc="a")
    await Tournament.objects.create(name="1", desc="b")
    await Tournament.objects.create(name="2", desc="c")
    tournaments = await Tournament.objects.annotate(count_name=Count("name")).distinct("name").order_by("name")
    assert [1, 1] == [tournaments[0].count_name, tournaments[0].count_name]


@test.requires_features(dialect="postgresql")
@pytest.mark.parametrize(
    "ordering_factory,expected",
    [
        (lambda: F("intnum_null").asc(nulls_first=True), [2, 1, 3, 4]),
        (lambda: F("intnum_null").asc(nulls_last=True), [1, 3, 4, 2]),
        (lambda: F("intnum_null").desc(nulls_first=True), [2, 4, 3, 1]),
        (lambda: F("intnum_null").desc(nulls_last=True), [4, 3, 1, 2]),
    ],
)
@pytest.mark.asyncio
async def test_distinct_on_with_explicit_null_placement(db, ordering_factory, expected):
    """DISTINCT ON keeps the first row of each group under the given ordering - NULLs form their own
    group, and its position follows the explicit placement."""
    for number, score in [(1, 1), (2, None), (3, 2), (4, 3), (5, None)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)

    rows = await IntFields.objects.all().distinct("intnum_null").order_by(ordering_factory(), "intnum")
    assert [row.intnum for row in rows] == expected


@pytest.mark.asyncio
async def test_distinct_on_with_null_placement_still_requires_matching_leading_field(db):
    with pytest.raises(QueryError, match=r"distinct\(\*fields\) must match"):
        await IntFields.objects.all().distinct("intnum_null").order_by(F("intnum").asc(nulls_last=True))


@pytest.mark.asyncio
async def test_distinct_on_default_ordered(db):
    await DefaultOrdered.objects.create(one="1", second=1)
    await DefaultOrdered.objects.create(one="2", second=2)
    assert [row.one for row in await DefaultOrdered.objects.all().distinct("one")] == ["1", "2"]


@pytest.mark.asyncio
async def test_distinct_on_by_relation(db):
    author_1 = await Author.objects.create(name="1")
    author_2 = await Author.objects.create(name="1")
    await Book.objects.create(name="1", rating=1, subject="1", author=author_1)
    await Book.objects.create(name="2", rating=2, subject="2", author=author_2)
    books = await Book.objects.all().distinct("author__name")
    assert len(books) == 1


# ---------------------------------------------------------------------------
# DISTINCT ON validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_on_invalid_order_by(db):
    await Tournament.objects.create(name="1")
    with pytest.raises(QueryError, match=r"distinct\(\*fields\) must match"):
        await Tournament.objects.all().distinct("name").order_by("desc")


@pytest.mark.asyncio
async def test_distinct_on_gives_the_same_rows_on_every_database(db):
    """distinct(*fields) keeps the first row of each combination in the ordering - by DISTINCT ON
    on PostgreSQL, by ROW_NUMBER() = 1 in a subquery on a database without it."""
    for row_id, intnum, intnum_null in ((1, 5, 1), (2, 3, 1), (3, 9, 2), (4, 7, 2), (5, 1, None), (6, 2, None)):
        await IntFields.objects.create(id=row_id, intnum=intnum, intnum_null=intnum_null)
    lowest = IntFields.objects.distinct("intnum_null").order_by("intnum_null", "intnum")
    highest = IntFields.objects.distinct("intnum_null").order_by("intnum_null", "-intnum")
    # The NULL group comes where the database sorts NULLs.
    expected_lowest = [5, 2, 4] if db.get_connection().dialect.features.sorts_nulls_first else [2, 4, 5]
    assert [row.id for row in await lowest] == expected_lowest
    assert sorted(await highest.values_list("id", flat=True)) == [1, 3, 6]
    assert await lowest.count() == 3
    assert await lowest.exists()
    assert (await highest.first()).intnum in (5, 9, 2)
    assert len(await lowest[1:]) == 2
    assert sorted(row["intnum"] for row in await lowest.values("intnum")) == [1, 3, 7]
    # Several fields, and a relation.
    assert await IntFields.objects.distinct("intnum_null", "intnum").order_by("intnum_null", "intnum").count() == 6
    with pytest.raises(QueryError, match=r"distinct\(\*fields\) must match"):
        await IntFields.objects.distinct("intnum_null").order_by("intnum")
    with pytest.raises(QueryError, match="aggregate"):
        await lowest.aggregate(total=Count("id"))


@pytest.mark.asyncio
async def test_distinct_on_a_relation_on_every_database(db):
    first = await Tournament.objects.create(id=1, name="first")
    second = await Tournament.objects.create(id=2, name="second")
    for event_id, tournament in ((1, first), (2, first), (3, second)):
        await Event.objects.create(event_id=event_id, name=f"e{event_id}", tournament=tournament)
    latest = Event.objects.distinct("tournament").order_by("tournament", "-event_id")
    assert [event.event_id for event in await latest] == [2, 3]


async def create_events_of_two_tournaments() -> tuple[Tournament, Tournament]:
    first = await Tournament.objects.create(name="a")
    second = await Tournament.objects.create(name="b")
    for name in ("e1", "e2", "e3"):
        await Event.objects.create(name=name, tournament=first)
    for name in ("f1", "f2"):
        await Event.objects.create(name=name, tournament=second)
    return first, second


def get_latest_event_per_tournament():
    return Event.objects.order_by("tournament_id", "-name").distinct("tournament_id")


@pytest.mark.asyncio
async def test_distinct_on_as_a_subquery_on_every_database(db):
    await create_events_of_two_tournaments()
    for latest in (get_latest_event_per_tournament(), get_latest_event_per_tournament().values("event_id")):
        names = await Event.objects.filter(event_id__in=latest).order_by("name").values_list("name", flat=True)
        assert names == ["e3", "f2"]
    first_latest = await Tournament.objects.annotate(
        latest=Subquery(get_latest_event_per_tournament().values("name")[:1])
    ).values_list("latest", flat=True)
    assert set(first_latest) == {"e3"}


@pytest.mark.asyncio
async def test_distinct_on_branches_of_a_union_keep_their_ordering(db):
    first, second = await create_events_of_two_tournaments()
    names = await (
        get_latest_event_per_tournament()
        .filter(tournament=first)
        .values("name")
        .union(get_latest_event_per_tournament().filter(tournament=second).values("name"))
        .order_by("name")
    )
    assert names == [{"name": "e3"}, {"name": "f2"}]
    events = (
        await get_latest_event_per_tournament()
        .filter(tournament=first)
        .union(get_latest_event_per_tournament().filter(tournament=second))
    )
    assert sorted(event.name for event in events) == ["e3", "f2"]


@pytest.mark.asyncio
async def test_an_outer_reference_of_distinct_on_reaches_the_enclosing_query_on_every_database(db):
    await create_events_of_two_tournaments()
    latest_names = (
        Tournament.objects.annotate(
            latest=Subquery(
                get_latest_event_per_tournament().filter(tournament_id=OuterReference("id")).values("name")
            )
        )
        .order_by("name")
        .values_list("latest", flat=True)
    )
    assert await latest_names == ["e3", "f2"]
    # Without DISTINCT ON the rows are numbered one query deeper - OuterReference("name") still reads the
    # tournament's name, not the event's own field of that name.
    await Tournament.objects.create(name="f2")
    tournaments_named_after_a_latest_event = await Tournament.objects.filter(
        Exists(get_latest_event_per_tournament().filter(name=OuterReference("name")))
    ).values_list("name", flat=True)
    assert tournaments_named_after_a_latest_event == ["f2"]
    events_with_a_later_sibling = (
        await Event.objects.filter(
            Exists(
                Event.objects.filter(tournament_id=OuterReference("tournament_id"), name__gt=OuterReference("name"))
                .order_by("tournament_id", "-name")
                .distinct("tournament_id")
            )
        )
        .order_by("name")
        .values_list("name", flat=True)
    )
    assert events_with_a_later_sibling == ["e1", "e2", "f1"]


@pytest.mark.asyncio
async def test_distinct_on_invalid_default_ordered(db):
    await DefaultOrdered.objects.create(one="1", second=1)
    await DefaultOrdered.objects.create(one="2", second=2)
    with pytest.raises(QueryError, match=r"distinct\(\*fields\) must match"):
        await DefaultOrdered.objects.all().distinct("second")


@test.requires_features(supports_distinct_on=True)
@pytest.mark.asyncio
async def test_distinct_on_unknown_field_raises_field_error_before_the_ordering_check(db):
    with pytest.raises(FieldError, match="no_such_field"):
        await Tournament.objects.all().order_by("name").distinct("no_such_field")


@pytest.mark.asyncio
async def test_distinct_on_unknown_field_raises_field_error_before_the_ordering_check_on_every_database(db):
    """An unknown name is the same FieldError where DISTINCT ON is emulated - not a mismatch with
    the ordering, as on PostgreSQL."""
    with pytest.raises(FieldError, match="Unknown field no_such_field for model Tournament"):
        await Tournament.objects.all().order_by("name").distinct("no_such_field")
    with pytest.raises(FieldError, match="Unknown field no_such_field for model Tournament"):
        await Event.objects.all().order_by("name").distinct("tournament__no_such_field")
    await Tournament.objects.create(id=1, name="first")
    annotated = Tournament.objects.annotate(doubled=F("id") * 2).order_by("doubled").distinct("doubled")
    assert [tournament.id for tournament in await annotated] == [1]


@pytest.mark.asyncio
async def test_distinct_on_a_selected_annotation_with_parameters_on_every_database(db):
    """DISTINCT ON a selected annotation repeats its ORDER BY term - on PostgreSQL the SELECT
    alias, as its expression's own parameters would make the two expressions differ."""
    for row_id, intnum in ((1, 1), (2, 1), (3, 2)):
        await IntFields.objects.create(id=row_id, intnum=intnum)
    doubled = IntFields.objects.annotate(doubled=F("intnum") * 2).order_by("doubled", "-id").distinct("doubled")
    assert [(row.id, row.doubled) for row in await doubled] == [(2, 2), (3, 4)]
    assert await doubled.values("id", "doubled") == [{"id": 2, "doubled": 2}, {"id": 3, "doubled": 4}]
    assert await doubled.values_list("id", "doubled") == [(2, 2), (3, 4)]
    assert [row.id for row in await doubled[1:]] == [3]
    assert await doubled.count() == 2
    hidden = IntFields.objects.alias(doubled=F("intnum") * 2).order_by("doubled", "-id").distinct("doubled")
    assert await hidden.values_list("id", flat=True) == [2, 3]


@pytest.mark.asyncio
async def test_distinct_on_a_composite_primary_key_on_every_database(db):
    for thing_id, revision in ((1, 1), (1, 2), (2, 1)):
        await CompositePkThing.objects.create(thing_id=thing_id, revision=revision, name=f"{thing_id}.{revision}")
    things = CompositePkThing.objects.order_by("pk").distinct("pk")
    assert await things.values_list("thing_id", "revision") == [(1, 1), (1, 2), (2, 1)]
