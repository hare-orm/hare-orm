import datetime

import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.query.expressions import F, RawSQL, Subquery
from hare.query.functions import Avg, Count, Sum, Upper
from tests.shared_models.testmodels import Author, Book, DateFields, Event, JSONFields, Team, Tournament


@pytest_asyncio.fixture
async def group_by_data(db):
    """Set up Author and Book data for group_by tests."""
    a1 = await Author.objects.create(name="author1")
    a2 = await Author.objects.create(name="author2")
    books1 = [await Book.objects.create(name=f"book{i}", author=a1, rating=i) for i in range(10)]
    books2 = [await Book.objects.create(name=f"book{i}", author=a2, rating=i) for i in range(5)]
    return {"a1": a1, "a2": a2, "books1": books1, "books2": books2}


# An aggregate per author over group_by_data: author1 has 10 books rated 0-9, author2 5 rated 0-4.
AGGREGATES = [
    pytest.param("count", lambda: Count("id"), 10, 5, id="count"),
    pytest.param("sum", lambda: Sum("rating"), 45.0, 10.0, id="sum"),
    pytest.param("avg", lambda: Avg("rating"), 4.5, 2.0, id="avg"),
]
# .values() and .values_list() - `read_row` gives a returned row as a tuple of the named fields.
VALUES_METHODS = [
    pytest.param("values", lambda fields, row: tuple(row[field] for field in fields), id="values"),
    pytest.param("values_list", lambda fields, row: tuple(row), id="values_list"),
]


@pytest.mark.parametrize(("method_name", "read_row"), VALUES_METHODS)
@pytest.mark.parametrize(
    ("group_field", "get_group_key"),
    [
        pytest.param("author_id", lambda author: author.pk, id="group_by"),
        pytest.param("author__name", lambda author: author.name, id="group_by_with_join"),
    ],
)
@pytest.mark.parametrize(("annotation_name", "make_aggregate", "first_value", "second_value"), AGGREGATES)
@pytest.mark.asyncio
async def test_an_aggregate_per_group(
    db,
    group_by_data,
    annotation_name,
    make_aggregate,
    first_value,
    second_value,
    group_field,
    get_group_key,
    method_name,
    read_row,
):
    fields = (group_field, annotation_name)
    rows = await getattr(
        Book.objects.annotate(**{annotation_name: make_aggregate()}).group_by(group_field), method_name
    )(*fields)

    assert sorted(read_row(fields, row) for row in rows) == sorted(
        [
            (get_group_key(group_by_data["a1"]), first_value),
            (get_group_key(group_by_data["a2"]), second_value),
        ]
    )


@pytest.mark.asyncio
async def test_count_and_exists_match_the_instances_of_a_group_by_queryset(db, group_by_data):
    """Model instances are always grouped by their primary key, so .group_by() doesn't change
    which rows `await qs` returns - count()/exists() agree with those rows."""
    queryset = Book.objects.annotate(count=Count("id")).group_by("author_id")
    assert len(await queryset) == 15
    assert await queryset.count() == 15

    exists = await queryset.exists()
    assert exists is True

    having_queryset = queryset.filter(count__gte=2)
    assert await having_queryset == []
    assert await having_queryset.count() == 0
    assert await having_queryset.exists() is False
    assert await having_queryset.update(name="never") == 0
    assert await having_queryset.delete() == 0

    no_match_exists = (
        await Book.objects.filter(name="does-not-exist").annotate(count=Count("id")).group_by("author_id").exists()
    )
    assert no_match_exists is False


@pytest.mark.parametrize(("method_name", "read_row"), VALUES_METHODS)
@pytest.mark.parametrize(
    ("annotation_name", "make_aggregate", "threshold", "expected"),
    [
        pytest.param("count", lambda: Count("id"), 6, 10, id="count"),
        pytest.param("sum", lambda: Sum("rating"), 11, 45.0, id="sum"),
        pytest.param("avg", lambda: Avg("rating"), 3, 4.5, id="avg"),
    ],
)
@pytest.mark.asyncio
async def test_a_filter_on_an_aggregate_per_group(
    db, group_by_data, annotation_name, make_aggregate, threshold, expected, method_name, read_row
):
    fields = ("author_id", annotation_name)
    rows = await getattr(
        Book.objects.annotate(**{annotation_name: make_aggregate()})
        .filter(**{f"{annotation_name}__gt": threshold})
        .group_by("author_id"),
        method_name,
    )(*fields)

    assert [read_row(fields, row) for row in rows] == [(group_by_data["a1"].pk, expected)]


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_count_values_list_group_by_does_not_duplicate_annotation_sql(db, group_by_data):
    """values_list() re-registers an annotation under its positional alias without dropping the
    original annotate()-given key, which previously caused _get_annotate() to select the
    same aggregate expression twice (once under each key) - wasted work, not wrong results
    (the correct value still landed at the right tuple position)."""
    qs = Book.objects.annotate(count=Count("id")).group_by("author_id").values_list("author_id", "count")
    sql = qs.sql(parameters_inline=True)
    # A column that holds no NULL: a dialect may count the rows instead of reading it.
    assert sql.count('COUNT("id")') + sql.count("COUNT(*)") == 1


# ============================================================================
# Auto GROUP BY on a purely local (no-join) aggregate - the outer gate on
# _apply_auto_group_by() used to also require a join/.having()/.order_by() before applying any
# GROUP BY at all, so a plain aggregate annotate() with none of those got no GROUP BY whatsoever:
# SQLite silently picked one arbitrary row per group (wrong result, no error), Postgres raised
# "must appear in the GROUP BY clause" outright.
# ============================================================================


@pytest.mark.asyncio
async def test_auto_group_by_local_aggregate_without_join_having_or_order_by(db):
    team_a = await Team.objects.create(name="Team A")
    team_b = await Team.objects.create(name="Team B")
    team_c = await Team.objects.create(name="Team C")

    teams = await Team.objects.all().annotate(total=Sum("id"))

    assert len(teams) == 3
    totals_by_id = {team.id: team.total for team in teams}
    assert totals_by_id == {team_a.id: team_a.id, team_b.id: team_b.id, team_c.id: team_c.id}


@pytest.mark.asyncio
async def test_implicit_group_by(db, group_by_data):
    ret = await Author.objects.annotate(count=Count("books")).filter(count__gt=6)
    assert ret[0].count == 10


@pytest.mark.asyncio
async def test_group_by_annotate_result(db, group_by_data):
    ret = (
        await Book.objects.annotate(upper_name=Upper("author__name"), count=Count("id"))
        .group_by("upper_name")
        .values("upper_name", "count")
    )
    assert sorted(ret, key=lambda x: x["upper_name"]) == sorted(
        [{"upper_name": "AUTHOR1", "count": 10}, {"upper_name": "AUTHOR2", "count": 5}],
        key=lambda x: x["upper_name"],
    )


@pytest.mark.asyncio
async def test_group_by_requiring_nested_joins(db):
    tournament_first = await Tournament.objects.create(name="Tournament 1", desc="d1")
    tournament_second = await Tournament.objects.create(name="Tournament 2", desc="d2")

    event_first = await Event.objects.create(name="1", tournament=tournament_first)
    event_second = await Event.objects.create(name="2", tournament=tournament_first)
    event_third = await Event.objects.create(name="3", tournament=tournament_second)

    team_first = await Team.objects.create(name="First", alias=2)
    team_second = await Team.objects.create(name="Second", alias=4)
    team_third = await Team.objects.create(name="Third", alias=5)

    await team_first.events.add(event_first)
    await team_second.events.add(event_second)
    await team_third.events.add(event_third)

    ret = (
        await Tournament.objects.annotate(avg=Avg("events__participants__alias"))
        .group_by("desc")
        .order_by("desc")
        .values("desc", "avg")
    )
    assert ret == [{"avg": 3, "desc": "d1"}, {"avg": 5, "desc": "d2"}]


@pytest.mark.asyncio
async def test_group_by_ambigious_column(db):
    tournament_first = await Tournament.objects.create(name="Tournament 1")
    tournament_second = await Tournament.objects.create(name="Tournament 2")

    await Event.objects.create(name="1", tournament=tournament_first)
    await Event.objects.create(name="2", tournament=tournament_first)
    await Event.objects.create(name="3", tournament=tournament_second)

    base_query = Tournament.objects.annotate(event_count=Count("events")).group_by("name").order_by("name")
    ret = await base_query.values("name", "event_count")
    assert ret == [
        {"event_count": 2, "name": "Tournament 1"},
        {"event_count": 1, "name": "Tournament 2"},
    ]

    ret = await base_query.values_list("name", "event_count")
    assert ret == [("Tournament 1", 2), ("Tournament 2", 1)]


@pytest.mark.asyncio
async def test_group_by_nested_column(db):
    tournament_first = await Tournament.objects.create(name="A")
    tournament_second = await Tournament.objects.create(name="B")

    await Event.objects.create(name="1", tournament=tournament_first)
    await Event.objects.create(name="2", tournament=tournament_first)
    await Event.objects.create(name="3", tournament=tournament_first)
    await Event.objects.create(name="4", tournament=tournament_second)

    base_query = (
        Event.objects.annotate(count=Count("event_id")).group_by("tournament__name").order_by("-tournament__name")
    )
    ret = await base_query.values("tournament__name", "count")
    assert ret == [
        {"count": 1, "tournament__name": "B"},
        {"count": 3, "tournament__name": "A"},
    ]

    ret = await base_query.values_list("tournament__name", "count")
    assert ret == [("B", 1), ("A", 3)]


@pytest.mark.asyncio
async def test_group_by_id_with_nested_filter(db, group_by_data):
    books1 = group_by_data["books1"]

    ret = await Book.objects.filter(author__name="author1").group_by("id").values_list("id")
    assert set(ret) == {(book.id,) for book in books1}


@pytest.mark.asyncio
async def test_select_subquery_with_group_by(db, group_by_data):
    a1 = group_by_data["a1"]
    a2 = group_by_data["a2"]

    subquery = Subquery(Book.objects.all().group_by("rating").order_by("-rating").limit(1).values("rating"))
    ret = await Author.objects.annotate(top_rating=subquery).order_by("id").values_list("name", "top_rating")
    assert ret == [(a1.name, 9.0), (a2.name, 9.0)]


@pytest.mark.asyncio
async def test_group_by_and_order_by_unselected_annotation_with_parameter(db, group_by_data):
    """group_by() and order_by() of an annotation carrying a bind parameter, left out of
    values_list(): its expression is rendered in both clauses, and Postgres only accepts the
    ORDER BY one when it carries the same parameter as the GROUP BY one."""
    arithmetic = (
        await Book.objects.annotate(rating_bucket=F("rating") / 5)
        .annotate(count=Count("id"))
        .group_by("rating_bucket")
        .order_by("rating_bucket")
        .values_list("count", flat=True)
    )
    raw = (
        await Book.objects.annotate(is_high=RawSQL("rating > %s", [6]))
        .annotate(count=Count("id"))
        .group_by("is_high")
        .order_by("is_high")
        .values_list("count", flat=True)
    )

    assert sum(arithmetic) == 15 and len(arithmetic) == 10
    assert raw == [12, 3]


@pytest.mark.asyncio
async def test_group_by_unselected_annotation_is_not_served_from_a_stale_cache(db, group_by_data):
    """The GROUP BY/ORDER BY expression of an unselected annotation isn't refreshed from the
    query shape cache - a second call with another literal must not reuse the first one's."""
    counts_by_multiplier = {}
    for multiplier in (1, 0):
        counts_by_multiplier[multiplier] = (
            await Book.objects.annotate(rating_bucket=F("rating") * multiplier)
            .annotate(count=Count("id"))
            .group_by("rating_bucket")
            .order_by("rating_bucket")
            .values_list("count", flat=True)
        )

    assert len(counts_by_multiplier[1]) == 10
    assert counts_by_multiplier[0] == [15]


@pytest.mark.asyncio
async def test_a_grouped_query_orders_by_its_groups(db, group_by_data):
    grouped = Book.objects.annotate(count=Count("id")).group_by("author_id")

    by_author = await grouped.order_by("-count", (F("author_id") * 1).asc()).values_list("author_id", "count")

    assert by_author == [(group_by_data["a1"].id, 10), (group_by_data["a2"].id, 5)]


@pytest.mark.asyncio
async def test_columns_of_a_grouped_primary_key_are_read(db, group_by_data):
    """Grouped by its primary key, a row's other columns are one per group - read on every
    database, ClickHouse included, which is told to group by them too."""
    rows = await Book.objects.annotate(count=Count("id")).group_by("id").order_by("id").values_list("name", "count")

    assert rows == [(book.name, 1) for book in (*group_by_data["books1"], *group_by_data["books2"])]


@pytest.mark.asyncio
async def test_group_by_and_values_again_take_a_path_inside_a_value(db):
    """group_by() of a date part or a JSON path, and values() again over rows grouped by one, raised
    "Unknown field 'date__year'"."""
    for row_id, date in (
        (1, datetime.date(2020, 1, 1)),
        (2, datetime.date(2020, 6, 1)),
        (3, datetime.date(2021, 1, 1)),
    ):
        await DateFields.objects.create(id=row_id, date=date)
    by_year = DateFields.objects.values("date__year").annotate(count=Count("id")).order_by("date__year")
    assert await by_year.values_list("count", flat=True) == [2, 1]
    assert await DateFields.objects.group_by("date__year").annotate(count=Count("id")).order_by("count").values_list(
        "count", flat=True
    ) == [1, 2]
    for row_id, key in ((1, "a"), (2, "a"), (3, "b")):
        await JSONFields.objects.create(id=row_id, data={"k": key})
    by_key = JSONFields.objects.group_by("data__k").annotate(count=Count("id")).order_by("count")
    assert await by_key.values_list("count", flat=True) == [1, 2]
    assert await JSONFields.objects.values("data__k").annotate(count=Count("id")).order_by("data__k").values_list(
        "count", flat=True
    ) == [2, 1]
