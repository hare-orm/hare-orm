from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from hare.contrib import test
from hare.contrib.test import requires_features
from hare.exceptions import FieldError, QueryError
from hare.query.expressions import Case, Exists, F, OuterReference, Q, Subquery, Value, When, Window
from hare.query.expressions.aggregate_paths import AggregatedMultiValuedPaths
from hare.query.functions import Avg, Coalesce, Concat, Count, Lower, Max, Min, Sum, Trim
from hare.query.functions.window import RowNumber
from hare.query.statements.building.query_joins import QueryJoins
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    AggregationOrder,
    AggregationProduct,
    AggregationReview,
    AggregationShop,
    AggregationTag,
    Author,
    Book,
    CompositePkOwningFK,
    DecimalFields,
    Event,
    ExpressionTypeParity,
    FloatFields,
    IntFields,
    JSONFields,
    MinRelation,
    Team,
    Tournament,
    UniqueName,
    UniqueTogetherFields,
    UniqueTogetherFieldsWithFK,
    ValidatorModel,
)


@pytest.mark.asyncio
async def test_aggregation(db):
    tournament = Tournament(name="New Tournament")
    await tournament.save()
    await Tournament.objects.create(name="Second tournament")
    await Event(name="Without participants", tournament_id=tournament.id).save()
    event = Event(name="Test", tournament_id=tournament.id)
    await event.save()
    participants = []
    for i in range(2):
        team = Team(name=f"Team {(i + 1)}")
        await team.save()
        participants.append(team)
    await event.participants.add(participants[0], participants[1])
    await event.participants.add(participants[0], participants[1])

    tournaments_with_count = (
        await Tournament.objects.all().annotate(events_count=Count("events")).filter(events_count__gte=1)
    )
    assert len(tournaments_with_count) == 1
    assert tournaments_with_count[0].events_count == 2

    event_with_lowest_team_id = (
        await Event.objects.filter(event_id=event.event_id).first().annotate(lowest_team_id=Min("participants__id"))
    )
    assert event_with_lowest_team_id.lowest_team_id == participants[0].id

    ordered_tournaments = (
        await Tournament.objects.all().annotate(events_count=Count("events")).order_by("events_count")
    )
    assert len(ordered_tournaments) == 2
    assert ordered_tournaments[1].id == tournament.id
    event_with_annotation = await Event.objects.all().annotate(tournament_test_id=Sum("tournament__id")).first()
    assert event_with_annotation.tournament_test_id == event_with_annotation.tournament_id

    with pytest.raises(FieldError, match="name__id not resolvable"):
        await Event.objects.all().annotate(tournament_test_id=Sum("name__id")).first()


@pytest.mark.asyncio
async def test_annotate_colliding_with_real_field_raises_on_full_fetch(db):
    """An annotate() key sharing a name with a real model field corrupts full-model hydration
    (row-lookup-by-name picks whichever of the two same-keyed SELECT columns the driver
    happens to return - the FIRST on sqlite3.Row, the LAST on asyncpg.Record - silently
    dropping the annotation on one dialect and overwriting the real field's value with it on
    the other). Must raise loudly instead of ever hydrating a corrupted instance."""
    await Team.objects.create(name="Team A")

    with pytest.raises(FieldError, match="name"):
        await Team.objects.all().annotate(name=Count("events__event_id"))


@pytest.mark.asyncio
async def test_annotate_colliding_with_real_field_raises_for_values_too(db):
    """Every other reference to the name (another annotation's F(), a filter, an ordering) would
    read the annotation instead of the field - rejected for .values()/.values_list() as well."""
    with pytest.raises(FieldError, match="conflict with field"):
        Team.objects.all().annotate(name=Count("events__event_id"))


@pytest.mark.asyncio
async def test_alias_not_included_in_select_but_usable_in_filter(db):
    """.alias() computes an expression usable in filter()/order_by(), like .annotate(), but -
    unlike .annotate() - never adds it to the SELECT list on its own."""
    await Tournament.objects.create(id=1, name="T1")
    await Tournament.objects.create(id=2, name="T2")

    qs = Tournament.objects.all().alias(shifted=F("id") + 10).filter(shifted__gt=11)
    select_list = qs.sql().split(" FROM")[0]
    assert '"shifted"' not in select_list

    result = await qs
    assert [t.name for t in result] == ["T2"]
    assert not hasattr(result[0], "shifted")


@pytest.mark.asyncio
async def test_alias_usable_in_order_by(db):
    await Tournament.objects.create(id=1, name="T1")
    await Tournament.objects.create(id=2, name="T2")

    result = await Tournament.objects.all().alias(shifted=F("id") * -1).order_by("shifted")
    assert [t.name for t in result] == ["T2", "T1"]


@pytest.mark.asyncio
async def test_alias_reappears_via_values(db):
    """An .alias() key must produce the exact same value as an equivalent .annotate() key once
    it's explicitly named in .values()/.values_list() - the only difference is whether it's
    included in an unqualified .values()/.values_list() with no args."""
    await Tournament.objects.create(id=1, name="T1")

    via_alias = await Tournament.objects.all().alias(shifted=F("id") + 1).values("shifted")
    via_annotate = await Tournament.objects.all().annotate(shifted=F("id") + 1).values("shifted")
    assert via_alias == via_annotate == [{"shifted": 2}]

    via_alias_list = await Tournament.objects.all().alias(shifted=F("id") + 1).values_list("shifted", flat=True)
    via_annotate_list = await Tournament.objects.all().annotate(shifted=F("id") + 1).values_list("shifted", flat=True)
    assert via_alias_list == via_annotate_list == [2]


@pytest.mark.asyncio
async def test_circular_annotation_reference_raises_configuration_error(db):
    """F.get_result()'s "reference to another annotation" branch used to recurse with no cycle
    guard at all - annotate(a=F("b")+1, b=F("a")+1) resolving `a` recurses into resolving `b`,
    which recurses back into `a`, and so on until the interpreter's own stack limit crashes it
    with a bare RecursionError, unlike every other nonsense-query shape in this area (a window
    function referencing another window function's alias, filtering directly on a window
    annotation) which all raise a clean QueryError instead."""
    await Tournament.objects.create(id=1, name="T1")

    with pytest.raises(QueryError, match="Circular annotation reference"):
        await Tournament.objects.all().annotate(a=F("b") + 1, b=F("a") + 1).values("a", "b")


@pytest.mark.asyncio
async def test_alias_excluded_from_unqualified_values_and_values_list(db):
    """An .alias() key must NOT appear in a bare .values()/.values_list() (no args) - only an
    .annotate() key is included there by default."""
    await Tournament.objects.create(id=1, name="T1")

    rows = await Tournament.objects.all().alias(shifted=F("id") + 1).values()
    assert "shifted" not in rows[0]

    rows_list = await Tournament.objects.all().alias(shifted=F("id") + 1).values_list()
    assert len(rows_list[0]) == len(Tournament._meta.db_fields)


@pytest.mark.asyncio
async def test_alias_colliding_with_real_field_name_raises(db):
    """An .alias() key named after a field would shadow the field in every other reference just
    like an .annotate() key."""
    with pytest.raises(FieldError, match="conflict with field"):
        Tournament.objects.all().alias(name=F("id"))


@pytest.mark.asyncio
async def test_alias_combined_with_select_related_and_prefetch(db):
    author = await Author.objects.create(name="Author One")
    await Book.objects.create(name="Cheap", author=author, rating=1)
    await Book.objects.create(name="Pricey", author=author, rating=9)

    via_select_related = (
        await Book.objects.all()
        .alias(rating_delta=F("rating") - 5)
        .filter(rating_delta__lt=0)
        .select_related("author")
    )
    assert [b.name for b in via_select_related] == ["Cheap"]
    assert via_select_related[0].author.name == "Author One"

    via_prefetch = await Author.objects.all().alias(x=F("id") + 1).prefetch_related("books")
    assert len(via_prefetch[0].books) == 2


@pytest.mark.asyncio
async def test_nested_aggregation_in_annotation(db):
    tournament = await Tournament.objects.create(name="0")
    await Tournament.objects.create(name="1")
    event = await Event.objects.create(name="2", tournament=tournament)

    team_first = await Team.objects.create(name="First")
    team_second = await Team.objects.create(name="Second")

    await event.participants.add(team_second)
    await event.participants.add(team_first)

    tournaments = await Tournament.objects.annotate(events_participants_count=Count("events__participants")).filter(
        id=tournament.id
    )
    assert tournaments[0].events_participants_count == 2


@pytest.mark.asyncio
async def test_aggregation_with_distinct(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)

    tournament_2 = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Event 1", tournament=tournament_2)
    await Event.objects.create(name="Event 2", tournament=tournament_2)
    await Event.objects.create(name="Event 3", tournament=tournament_2)
    await MinRelation.objects.create(tournament=tournament_2)
    await MinRelation.objects.create(tournament=tournament_2)

    school_with_distinct_count = (
        await Tournament.objects.filter(id=tournament_2.id)
        .annotate(
            events_count=Count("events", distinct=True),
            minrelations_count=Count("minrelations", distinct=True),
        )
        .first()
    )

    assert school_with_distinct_count.events_count == 3
    assert school_with_distinct_count.minrelations_count == 2


@pytest.mark.asyncio
async def test_aggregation_over_two_relations_without_distinct_raises_clear_error(db):
    """Aggregating two DIFFERENT to-many relations in the same .annotate() call, without
    distinct=True on at least one of them, used to silently return a wrong (cross-multiplied)
    number instead of raising - e.g. 2 events and 3 minrelations both came back as 6 (2*3)."""
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)

    with pytest.raises(QueryError, match="more than one to-many relation"):
        await (
            Tournament.objects.filter(id=tournament.id)
            .annotate(events_count=Count("events"), minrelations_count=Count("minrelations"))
            .first()
        )


@pytest.mark.asyncio
async def test_aggregation_over_two_relations_one_non_distinct_still_raises(db):
    """Only one of the two aggregates missing distinct=True is enough to make the combination
    unsafe - the non-distinct one is still cross-multiplied by the other relation's own rows."""
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)

    with pytest.raises(QueryError, match="more than one to-many relation"):
        await (
            Tournament.objects.filter(id=tournament.id)
            .annotate(events_count=Count("events", distinct=True), minrelations_count=Count("minrelations"))
            .first()
        )


@pytest.mark.asyncio
async def test_exists_over_two_relations_without_distinct_does_not_raise(db):
    """The two-to-many-relations guard exists to protect a caller that reads the aggregate's
    actual (cross-multiplied) VALUE - .exists() only reads `bool(result)`, so the same shape that
    .count()/.first() must reject is safe here: the LEFT OUTER JOINs involved can duplicate rows
    but can never turn a real match into zero rows."""
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)

    assert (
        await Tournament.objects.filter(id=tournament.id)
        .annotate(events_count=Count("events"), minrelations_count=Count("minrelations"))
        .exists()
    ) is True

    assert (
        await Tournament.objects.filter(name="No such tournament")
        .annotate(events_count=Count("events"), minrelations_count=Count("minrelations"))
        .exists()
    ) is False


@pytest.mark.asyncio
async def test_count_over_two_relations_without_distinct_still_raises(db):
    """Unlike .exists(), .count() genuinely computes COUNT(*) over the cross-multiplied JOIN
    rows - the guard must still reject this shape for .count(), the same as for .first()/
    .values()."""
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)
    await MinRelation.objects.create(tournament=tournament)

    with pytest.raises(QueryError, match="more than one to-many relation"):
        await (
            Tournament.objects.filter(id=tournament.id)
            .annotate(events_count=Count("events"), minrelations_count=Count("minrelations"))
            .count()
        )


@pytest.mark.asyncio
async def test_exists_sql_drops_unused_annotation_select_columns(db):
    """.annotate(cnt=Count(...)).exists() must generate a plain SELECT 1 ... LIMIT 1 - the
    annotate()'d aggregate's own value is never read by exists(), so it must not be computed/
    selected at all, mirroring CountQuery's identical kept_selects cleanup."""
    tournament = await Tournament.objects.create(name="New Tournament")
    sql = Tournament.objects.filter(id=tournament.id).annotate(events_count=Count("events")).exists().sql()
    assert "events_count" not in sql
    assert "COUNT" not in sql.upper()


@pytest.mark.asyncio
async def test_aggregation_over_single_relation_unaffected(db):
    """The new guard must not become overzealous - a single aggregated to-many relation (no
    distinct= needed) must keep working exactly as before."""
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)

    result = await Tournament.objects.filter(id=tournament.id).annotate(events_count=Count("events")).first()
    assert result.events_count == 2


@pytest.mark.asyncio
async def test_aggregation_with_filter(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)
    await Event.objects.create(name="Event 3", tournament=tournament)

    tournament_with_filter = (
        await Tournament.objects.all()
        .annotate(
            all=Count("events", _filter=Q(name="New Tournament")),
            one=Count("events", _filter=Q(events__name="Event 1")),
            two=Count("events", _filter=Q(events__name__not="Event 1")),
        )
        .first()
    )

    assert tournament_with_filter.all == 3
    assert tournament_with_filter.one == 1
    assert tournament_with_filter.two == 2


@pytest.mark.asyncio
async def test_aggregation_with_filter_and_f_field(db):
    """_filter= must apply equally whether the aggregated field is a plain string (already
    covered by test_aggregation_with_filter) or an F()/CombinedExpression - the CASE WHEN
    filter-wrapping used to live only in the code path a plain string field name reaches, so an
    F()-wrapped field silently built an unfiltered aggregate instead. Same setup/assertions as
    test_aggregation_with_filter, just with F("events") instead of the bare "events" string."""
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)
    await Event.objects.create(name="Event 3", tournament=tournament)

    tournament_with_filter = (
        await Tournament.objects.all()
        .annotate(
            all=Count(F("events"), _filter=Q(name="New Tournament")),
            one=Count(F("events"), _filter=Q(events__name="Event 1")),
            two=Count(F("events"), _filter=Q(events__name__not="Event 1")),
        )
        .first()
    )

    assert tournament_with_filter.all == 3
    assert tournament_with_filter.one == 1
    assert tournament_with_filter.two == 2


@pytest.mark.asyncio
async def test_group_aggregation(db):
    author = await Author.objects.create(name="Some One")
    await Book.objects.create(name="First!", author=author, rating=4)
    await Book.objects.create(name="Second!", author=author, rating=3)
    await Book.objects.create(name="Third!", author=author, rating=3)

    authors = await Author.objects.all().annotate(average_rating=Avg("books__rating"))
    assert authors[0].average_rating == pytest.approx(3.3333333333, rel=1e-5)

    authors = await Author.objects.all().annotate(average_rating=Avg("books__rating")).values()
    assert authors[0]["average_rating"] == pytest.approx(3.3333333333, rel=1e-5)

    authors = (
        await Author.objects.all().annotate(average_rating=Avg("books__rating")).values("id", "name", "average_rating")
    )
    assert authors[0]["average_rating"] == pytest.approx(3.3333333333, rel=1e-5)

    authors = await Author.objects.all().annotate(average_rating=Avg("books__rating")).values_list()
    assert authors[0][2] == pytest.approx(3.3333333333, rel=1e-5)

    authors = (
        await Author.objects.all()
        .annotate(average_rating=Avg("books__rating"))
        .values_list("id", "name", "average_rating")
    )
    assert authors[0][2] == pytest.approx(3.3333333333, rel=1e-5)


@pytest.mark.asyncio
async def test_group_aggregation_selecting_a_joined_column(db):
    """The implicit GROUP BY an aggregate annotate() builds when the caller didn't write an
    explicit .group_by() used to only ever include the base table's OWN columns - a joined
    table's column that's ALSO selected (via .values("<relation>__<field>", ...) or
    .select_related()) was silently left out of the GROUP BY. SQLite's relaxed GROUP BY semantics
    tolerated this (picking an arbitrary value per group instead of erroring); Postgres rejects
    it outright ("column ... must appear in the GROUP BY clause or be used in an aggregate
    function")."""
    author_one = await Author.objects.create(name="Author One")
    author_two = await Author.objects.create(name="Author Two")
    await Book.objects.create(name="A1", author=author_one, rating=4)
    await Book.objects.create(name="A2", author=author_one, rating=2)
    await Book.objects.create(name="B1", author=author_two, rating=5)

    rows = await Book.objects.all().values("author__name").annotate(book_count=Count("id"))
    assert {(row["author__name"], row["book_count"]) for row in rows} == {
        ("Author One", 2),
        ("Author Two", 1),
    }

    # select_related() alongside a plain (non-values()) annotate() computes the aggregate
    # per resulting row (grouped by that row's own primary key, same as an aggregate annotate()
    # with no select_related() at all already does) - select_related()'s joined columns must be
    # grouped by too, or Postgres rejects the query the same way.
    books = await Book.objects.all().annotate(book_count=Count("id")).select_related("author")
    assert {(book.author.name, book.book_count) for book in books} == {
        ("Author One", 1),
        ("Author Two", 1),
    }


@pytest.mark.asyncio
async def test_nested_functions(db):
    author = await Author.objects.create(name="Some One")
    await Book.objects.create(name="First!", author=author, rating=4)
    await Book.objects.create(name="Second!", author=author, rating=3)
    await Book.objects.create(name="Third!", author=author, rating=3)
    ret = await Book.objects.all().values("author_id").annotate(max_name=Lower(Max("name"))).values("max_name")
    assert ret == [{"max_name": "third!"}]


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_concat_functions(db):
    author = await Author.objects.create(name="Some One")
    await Book.objects.create(name="Physics Book", author=author, rating=4, subject="physics ")
    await Book.objects.create(name="Mathematics Book", author=author, rating=3, subject=" mathematics")
    await Book.objects.create(name="No-subject Book", author=author, rating=3)
    ret = (
        await Book.objects.all()
        .values("author_id")
        .annotate(long_info=Max(Concat("name", "(", Coalesce(Trim("subject"), "others"), ")")))
        .values("long_info")
    )
    assert ret == [{"long_info": "Physics Book(physics)"}]


@pytest.mark.asyncio
async def test_concat_non_string_literal(db):
    """Concat() with a non-string literal (int/Decimal/date) used to only work on
    sqlite/rust_pg - asyncpg raised `DataError: invalid input for query argument $1: 123
    (expected str, got int)` because every argument was blanket-cast to ::text, which makes
    Postgres describe the bind parameter as text while the bound value stayed a non-str
    Python type. Confirmed live; must give the identical result on all three backends."""
    author = await Author.objects.create(name="Some One")
    ret = (
        await Author.objects.filter(id=author.id)
        .annotate(
            concat_int=Concat("name", Value(123)),
            concat_decimal=Concat("name", Value(Decimal("4.50"))),
            concat_date=Concat("name", Value(date(2024, 1, 1))),
        )
        .values("concat_int", "concat_decimal", "concat_date")
    )
    assert ret == [
        {
            "concat_int": "Some One123",
            "concat_decimal": "Some One4.50",
            "concat_date": "Some One2024-01-01",
        }
    ]


@pytest.mark.asyncio
async def test_coalesce_incompatible_default_literal_skips_field_coercion(db):
    """Coalesce()'s output_field used to always come from its main field argument alone -
    Coalesce(intnum_null, Decimal("5.55")) then decoded the whole result through the
    inferred IntField regardless of the default's own type, raising `ValueError: invalid
    literal for int() with base 10: '5.55'` on SQLite and silently truncating the value to 5
    on asyncpg/rust_pg. Confirmed live; an incompatible default must now leave the result
    undecoded (the driver's own native value) instead of guessing wrong."""
    await IntFields.objects.create(intnum=1, intnum_null=None)
    rows = (
        await IntFields.objects.all().annotate(coalesced=Coalesce("intnum_null", Decimal("5.55"))).values("coalesced")
    )
    assert len(rows) == 1
    assert Decimal(str(rows[0]["coalesced"])) == Decimal("5.55")


@pytest.mark.asyncio
async def test_coalesce_incompatible_value_default_skips_field_coercion(db):
    """The same bug as above for a default wrapped in Value(...) - an Expression whose own
    output_field is None, which used to be treated as always compatible."""
    await IntFields.objects.create(intnum=1, intnum_null=None)
    await IntFields.objects.create(intnum=2, intnum_null=9)
    rows = (
        await IntFields.objects.all()
        .order_by("id")
        .annotate(coalesced=Coalesce("intnum_null", Value(Decimal("5.55"))))
        .values_list("coalesced", flat=True)
    )
    assert [Decimal(str(row)) for row in rows] == [Decimal("5.55"), Decimal("9")]


@pytest.mark.asyncio
async def test_coalesce_wider_numeric_default_keeps_decimal_field_decoding(db):
    """A DecimalField main field decodes an int/Decimal default itself (and keeps decoding its own
    non-NULL rows) - the result stays a Decimal for every row. A float default makes the whole
    result a float instead."""
    await DecimalFields.objects.create(
        decimal=Decimal("1"), decimal_nodec=Decimal("1"), decimal_null=Decimal("12.5000")
    )
    await DecimalFields.objects.create(decimal=Decimal("2"), decimal_nodec=Decimal("2"), decimal_null=None)
    for default, expected_default, expected_type in (
        (0.0, 0.0, float),
        (0, Decimal("0"), Decimal),
        (Decimal("1.25"), Decimal("1.25"), Decimal),
        (Value(0.5), 0.5, float),
    ):
        rows = (
            await DecimalFields.objects.all()
            .order_by("id")
            .annotate(coalesced=Coalesce("decimal_null", default))
            .values_list("coalesced", flat=True)
        )
        assert all(type(row) is expected_type for row in rows), (default, rows)
        assert rows == [expected_type("12.5"), expected_default], default


@pytest.mark.asyncio
async def test_coalesce_wider_numeric_default_keeps_float_field_decoding(db):
    await FloatFields.objects.create(floatnum=1.0, floatnum_null=1.5)
    await FloatFields.objects.create(floatnum=2.0, floatnum_null=None)
    for default, expected_default in ((0, 0.0), (2.5, 2.5), (Decimal("2.5"), 2.5), (Value(Decimal("2.5")), 2.5)):
        rows = (
            await FloatFields.objects.all()
            .order_by("id")
            .annotate(coalesced=Coalesce("floatnum_null", default))
            .values_list("coalesced", flat=True)
        )
        assert all(isinstance(row, float) for row in rows), (default, rows)
        assert rows == [1.5, expected_default], default


@pytest.mark.asyncio
async def test_count_after_aggregate(db):
    author = await Author.objects.create(name="1")
    await Book.objects.create(name="First!", author=author, rating=4)
    await Book.objects.create(name="Second!", author=author, rating=3)
    await Book.objects.create(name="Third!", author=author, rating=3)

    author2 = await Author.objects.create(name="2")
    await Book.objects.create(name="F-2", author=author2, rating=3)
    await Book.objects.create(name="F-3", author=author2, rating=3)

    author3 = await Author.objects.create(name="3")
    await Book.objects.create(name="F-4", author=author3, rating=3)
    await Book.objects.create(name="F-5", author=author3, rating=2)
    ret = (
        await Author.objects.all().annotate(average_rating=Avg("books__rating")).filter(average_rating__gte=3).count()
    )

    assert ret == 2


@pytest.mark.asyncio
async def test_count_resolves_a_bare_string_referencing_another_annotation(db):
    """Count("alias") (a bare string, not F("alias")) used to always fall through to
    LookupPaths.get_nested_field(), which only ever resolves real model fields/relations -
    raising a confusing "<alias> not resolvable" FieldError instead of finding the earlier
    .annotate()'d alias, unlike F("alias") in the exact same position (see F.get_result()'s own
    "reference to another annotation" branch, which Function._get_nested_field() now mirrors)."""
    author = await Author.objects.create(name="1")
    await Book.objects.create(name="First!", author=author, rating=4)
    await Book.objects.create(name="Second!", author=author, rating=2)
    await Book.objects.create(name="Third!", author=author, rating=5)

    result = (
        await Author.objects.filter(pk=author.pk)
        .annotate(is_highly_rated=Case(When(books__rating__gte=4, then=1), default=None))
        .annotate(highly_rated_count=Count("is_highly_rated"))
        .values("highly_rated_count")
    )

    assert result == [{"highly_rated_count": 2}]


@pytest.mark.asyncio
async def test_exist_after_aggregate(db):
    author = await Author.objects.create(name="1")
    await Book.objects.create(name="First!", author=author, rating=4)
    await Book.objects.create(name="Second!", author=author, rating=3)
    await Book.objects.create(name="Third!", author=author, rating=3)

    ret = (
        await Author.objects.all().annotate(average_rating=Avg("books__rating")).filter(average_rating__gte=3).exists()
    )

    assert ret is True

    ret = (
        await Author.objects.all().annotate(average_rating=Avg("books__rating")).filter(average_rating__gte=4).exists()
    )
    assert ret is False


@pytest.mark.asyncio
async def test_count_after_aggregate_m2m(db):
    tournament = await Tournament.objects.create(name="1")
    event1 = await Event.objects.create(name="First!", tournament=tournament)
    event2 = await Event.objects.create(name="Second!", tournament=tournament)
    event3 = await Event.objects.create(name="Third!", tournament=tournament)
    event4 = await Event.objects.create(name="Fourth!", tournament=tournament)

    team1 = await Team.objects.create(name="1")
    team2 = await Team.objects.create(name="2")
    team3 = await Team.objects.create(name="3")

    await event1.participants.add(team1, team2, team3)
    await event2.participants.add(team1, team2)
    await event3.participants.add(team1)
    await event4.participants.add(team1, team2, team3)

    base_query = (
        Event.objects.filter(participants__id__in=[team1.id, team2.id, team3.id])
        .annotate(count=Count("participants"))
        .filter(count=3)
    )
    result = await base_query.prefetch_related("participants")
    assert len(result) == 2

    # count() cannot be combined with prefetch_related() - reused on the same base
    # filters/annotations, without the prefetch, instead.
    res = await base_query.count()
    assert res == 2


@pytest.mark.asyncio
async def test_where_and_having(db):
    author = await Author.objects.create(name="1")
    await Book.objects.create(name="First!", author=author, rating=4)
    await Book.objects.create(name="Second!", author=author, rating=3)
    await Book.objects.create(name="Third!", author=author, rating=3)

    query = (
        Book.objects.exclude(name="First!").values("author_id").annotate(avg_rating=Avg("rating")).values("avg_rating")
    )
    result = await query
    assert len(result) == 1
    assert result[0]["avg_rating"] == 3


@pytest.mark.asyncio
async def test_count_without_matching(db) -> None:
    await Tournament.objects.create(name="Test")

    query = Tournament.objects.annotate(events_count=Count("events")).filter(events_count__gt=0).count()
    result = await query
    assert result == 0


@pytest.mark.asyncio
async def test_int_sum_on_models_with_validators(db) -> None:
    await ValidatorModel.objects.create(max_value=2)
    await ValidatorModel.objects.create(max_value=2)

    query = ValidatorModel.objects.all().values("regex").annotate(sum=Sum("max_value")).values("sum")
    result = await query
    assert result == [{"sum": 4}]


@pytest.mark.asyncio
async def test_int_sum_math_on_models_with_validators(db) -> None:
    await ValidatorModel.objects.create(max_value=4)
    await ValidatorModel.objects.create(max_value=4)

    query = (
        ValidatorModel.objects.all().values("regex").annotate(sum=Sum(F("max_value") * F("max_value"))).values("sum")
    )
    result = await query
    assert result == [{"sum": 32}]


@pytest.mark.asyncio
async def test_decimal_sum_on_models_with_validators(db) -> None:
    await ValidatorModel.objects.create(min_value_decimal=2.0)

    query = ValidatorModel.objects.annotate(sum=Sum("min_value_decimal")).values("sum")
    result = await query
    assert result == [{"sum": Decimal("2.0")}]


@pytest.mark.asyncio
async def test_decimal_sum_with_math_on_models_with_validators(db) -> None:
    await ValidatorModel.objects.create(min_value_decimal=2.0)

    query = ValidatorModel.objects.annotate(
        sum=Sum(F("min_value_decimal") - F("min_value_decimal") * F("min_value_decimal"))
    ).values("sum")
    result = await query
    assert result == [{"sum": Decimal("-2.0")}]


@pytest.mark.asyncio
async def test_function_requiring_nested_joins(db):
    tournament = await Tournament.objects.create(name="Tournament")

    event_first = await Event.objects.create(name="1", tournament=tournament)
    event_second = await Event.objects.create(name="2", tournament=tournament)

    team_first = await Team.objects.create(name="First", alias=2)
    team_second = await Team.objects.create(name="Second", alias=10)

    await team_first.events.add(event_first)
    await event_second.participants.add(team_second)

    res = await Tournament.objects.annotate(avg=Avg("events__participants__alias")).values("avg")
    assert res == [{"avg": 6}]


@pytest.mark.asyncio
async def test_avg_of_int_field_keeps_fractional_part(db):
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=2)
    await IntFields.objects.create(intnum=2)

    res = await IntFields.objects.all().values("intnum_null").annotate(avg=Avg("intnum")).values("avg")

    assert res == [{"avg": pytest.approx(5 / 3)}]


@pytest.mark.asyncio
async def test_aggregate_multiple_metrics_returns_single_dict(db):
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=2)
    await IntFields.objects.create(intnum=3)

    result = await IntFields.objects.all().aggregate(total=Sum("intnum"), avg=Avg("intnum"))
    assert result == {"total": 6, "avg": 2.0}


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_aggregate_selects_no_base_table_columns(db):
    """Unlike .annotate()/.values(), .aggregate() must never touch the model at all - the
    generated SQL's own SELECT list must contain nothing but the requested aggregate
    expressions, no base-table columns."""
    sql = IntFields.objects.all().aggregate(total=Sum("intnum")).sql()
    select_list = sql.split(" FROM")[0]
    assert select_list == 'SELECT SUM("intnum") "total"'


@pytest.mark.asyncio
async def test_aggregate_combined_with_filter(db):
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=2)
    await IntFields.objects.create(intnum=3)

    result = await IntFields.objects.filter(intnum__gte=2).aggregate(total=Sum("intnum"), count=Count("intnum"))
    assert result == {"total": 5, "count": 2}


@pytest.mark.asyncio
async def test_aggregate_runs_over_the_group_by_rows(db):
    """.aggregate() still collapses to a single row after a .group_by(), computed over the grouped
    rows - one per distinct intnum - like Django's values().annotate().aggregate()."""
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=2)

    result = await IntFields.objects.all().group_by("intnum").aggregate(total=Sum("intnum"))
    assert result == {"total": 3}


@pytest.mark.asyncio
async def test_aggregate_empty_result_uses_sql_aggregate_semantics(db):
    """A plain SQL aggregate with no GROUP BY always produces exactly one row, even over zero
    matching rows - NULL for SUM/AVG, 0 for COUNT. Confirms .aggregate() doesn't special-case
    "no matches" itself, it just runs the real query and lets the database's own aggregate
    semantics decide - matching test_none_aggregate_is_none_without_querying's *different*
    short-circuit for .none() (which never reaches the database at all)."""
    await IntFields.objects.create(intnum=1)

    result = await IntFields.objects.filter(intnum__gte=100).aggregate(total=Sum("intnum"), count=Count("intnum"))
    assert result == {"total": None, "count": 0}


@pytest.mark.asyncio
async def test_aggregate_combined_with_related_field(db):
    tournament = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=tournament)
    await Event.objects.create(name="E2", tournament=tournament)

    result = await Tournament.objects.filter(id=tournament.id).aggregate(event_count=Count("events__event_id"))
    assert result == {"event_count": 2}


@pytest.mark.asyncio
async def test_aggregate_combined_with_prior_alias_and_filter(db):
    """A .filter() referencing a PRIOR .alias() key (before .aggregate() is called) must still
    resolve correctly - .aggregate() merges its own kwargs into a copy of the originating
    queryset's own annotations, never mutating it (mirrors .values()'s identical concern for a
    kwarg expression). The alias itself must stay out of the result dict - only the requested
    metric key is returned."""
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=5)
    await IntFields.objects.create(intnum=10)

    result = (
        await IntFields.objects.all()
        .alias(shifted=F("intnum") + 100)
        .filter(shifted__gt=104)
        .aggregate(total=Sum("intnum"))
    )
    assert result == {"total": 15}


@pytest.mark.asyncio
async def test_coalesce_of_json_field_decodes_through_the_fields_own_type(db):
    """Coalesce never set populate_field_object, so QuerySet._get_annotate() had nothing to
    decode the annotation's result through - a JSONField argument's own from_db_value()
    (decoding its stored text back into a dict) never ran, returning the raw driver value
    instead. Coalesce's result shares its first argument's type (every argument is expected to be
    type-compatible for a well-formed COALESCE), same as Max/Min/Avg/Sum already do via
    populate_field_object."""
    obj_null = await JSONFields.objects.create(data={"fallback": True}, data_null=None)
    obj_set = await JSONFields.objects.create(data={"fallback": True}, data_null={"y": 2})

    row_null = await JSONFields.objects.filter(id=obj_null.id).annotate(x=Coalesce("data_null", F("data"))).values("x")
    row_set = await JSONFields.objects.filter(id=obj_set.id).annotate(x=Coalesce("data_null", F("data"))).values("x")

    assert row_null == [{"x": {"fallback": True}}]
    assert isinstance(row_null[0]["x"], dict)
    assert row_set == [{"x": {"y": 2}}]
    assert isinstance(row_set[0]["x"], dict)


@pytest.mark.asyncio
async def test_combined_expression_arithmetic_decodes_through_the_operands_field_type(db):
    """CombinedExpression (F("decimal") + F("decimal")) computes an output_field internally
    (to reject mismatched operand types) but never set populate_field_object, so
    QuerySet._get_annotate() never captured it - a Decimal field's arithmetic result came back
    as a raw float instead of a Decimal. Checked on a FIRST (cache miss) and a structurally
    identical SECOND (cache hit) call."""
    a = await DecimalFields.objects.create(decimal=Decimal("1.5000"), decimal_nodec=Decimal("1"))
    b = await DecimalFields.objects.create(decimal=Decimal("2.5000"), decimal_nodec=Decimal("2"))

    first = await DecimalFields.objects.filter(id=a.id).annotate(total=F("decimal") + F("decimal")).values("total")
    second = await DecimalFields.objects.filter(id=b.id).annotate(total=F("decimal") + F("decimal")).values("total")

    assert first[0]["total"] == Decimal("3.0000")
    assert isinstance(first[0]["total"], Decimal)
    assert second[0]["total"] == Decimal("5.0000")
    assert isinstance(second[0]["total"], Decimal)


@pytest.mark.asyncio
async def test_annotate_referencing_an_earlier_annotation_keeps_its_decoded_type(db):
    """.annotate(total=Sum("decimal")).annotate(bumped=Coalesce(F("total"), 0)) - "bumped"
    references "total" (an earlier populate_field_object annotation) via F(). F.get_result()'s
    "reference to another annotation" branch never propagated output_field, so Coalesce's own
    populate_field_object machinery received output_field=None from its F("total") argument and
    couldn't inherit "total"'s real Decimal field. Checked on a FIRST (cache miss) and a
    structurally identical SECOND (cache hit) call."""
    a = await DecimalFields.objects.create(decimal=Decimal("1.5000"), decimal_nodec=Decimal("1"))
    b = await DecimalFields.objects.create(decimal=Decimal("2.5000"), decimal_nodec=Decimal("2"))

    first = (
        await DecimalFields.objects.filter(id=a.id)
        .annotate(total=Sum("decimal"))
        .annotate(bumped=Coalesce(F("total"), 0))
        .values("total", "bumped")
    )
    second = (
        await DecimalFields.objects.filter(id=b.id)
        .annotate(total=Sum("decimal"))
        .annotate(bumped=Coalesce(F("total"), 0))
        .values("total", "bumped")
    )

    assert isinstance(first[0]["total"], Decimal)
    assert isinstance(first[0]["bumped"], Decimal)
    assert isinstance(second[0]["total"], Decimal)
    assert isinstance(second[0]["bumped"], Decimal)


@pytest.mark.asyncio
async def test_aggregate_on_a_sliced_queryset_aggregates_the_slice(db):
    """Like Django, aggregate() of a sliced queryset aggregates the rows of the slice - it used to
    raise QueryError. A slice whose rows can repeat a primary key is still rejected."""
    from tests.testmodels import IntFields

    for n in (1, 2, 3, 4, 5):
        await IntFields.objects.create(intnum=n)

    assert await IntFields.objects.all().order_by("intnum").limit(2).aggregate(total=Sum("intnum")) == {"total": 3}
    assert await IntFields.objects.all().order_by("intnum").offset(1).aggregate(total=Sum("intnum")) == {"total": 14}
    assert await IntFields.objects.all().order_by("-intnum")[1:3].aggregate(
        total=Sum("intnum"), count=Count("id")
    ) == {
        "total": 7,
        "count": 2,
    }
    assert await IntFields.objects.all().order_by("intnum")[10:].aggregate(total=Sum("intnum")) == {"total": None}


@pytest.mark.asyncio
async def test_count_rejects_prefetch_related(db):
    """.count() used to silently drop a prior prefetch_related() call - the result is a plain
    int, not model instances, so there was nothing to actually attach the prefetched relation
    to; the prefetch queries just never ran and the caller got no signal anything was wrong."""
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Final", tournament_id=tournament.id)

    with pytest.raises(ValueError, match="prefetch_related"):
        Tournament.objects.all().prefetch_related("events").count()

    # Unaffected without a prior prefetch_related() call.
    assert await Tournament.objects.all().count() == 1


@pytest.mark.asyncio
async def test_exists_rejects_prefetch_related(db):
    """.exists() used to silently drop a prior prefetch_related() call - the result is a plain
    bool, not model instances, so there was nothing to actually attach the prefetched relation
    to; the prefetch queries just never ran and the caller got no signal anything was wrong."""
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Final", tournament_id=tournament.id)

    with pytest.raises(ValueError, match="prefetch_related"):
        Tournament.objects.all().prefetch_related("events").exists()

    # Unaffected without a prior prefetch_related() call.
    assert await Tournament.objects.all().exists() is True


@pytest.mark.asyncio
async def test_aggregate_rejects_prefetch_related(db):
    """.aggregate() used to silently drop a prior prefetch_related() call - the result is a
    plain dict, not model instances, so there was nothing to actually attach the prefetched
    relation to; the prefetch queries just never ran and the caller got no signal anything was
    wrong."""
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Final", tournament_id=tournament.id)

    with pytest.raises(ValueError, match="prefetch_related"):
        Tournament.objects.all().prefetch_related("events").aggregate(total=Count("id"))

    # Unaffected without a prior prefetch_related() call.
    result = await Tournament.objects.all().aggregate(total=Count("id"))
    assert result == {"total": 1}


@pytest.mark.asyncio
async def test_when_with_an_empty_q_or_no_condition_at_all_means_always_true(db):
    """When(Q(), then=...) (or When(then=...) with no args/kwargs at all) resolves its condition
    to an EmptyCriterion, which has no get_sql() of its own - rendering the CASE WHEN SQL crashed
    with a bare, undiagnosable NotImplementedError instead of treating a degenerate/no-op
    condition as "always true", the same top-level no-op semantics a bare Q() already has in a
    plain .filter(Q())."""
    from tests.testmodels import Event

    tournament = await Tournament.objects.create(name="T")
    await Event.objects.create(name="E1", tournament=tournament)

    always_true = await Event.objects.annotate(label=Case(When(Q(), then="always"), default="never")).values_list(
        "label", flat=True
    )
    assert always_true == ["always"]

    no_condition_at_all = await Event.objects.annotate(label=Case(When(then="always"), default="never")).values_list(
        "label", flat=True
    )
    assert no_condition_at_all == ["always"]


@pytest.mark.asyncio
async def test_conditional_aggregate_over_integer_literal_case_returns_the_same_type_on_every_backend(db):
    """Sum/Avg over a Case whose branches are all plain ints has no field to infer a type from -
    the raw driver type leaked through: int on SQLite, Decimal on Postgres (SUM/AVG of an
    integer is NUMERIC there). Sum must come back as an int and Avg as a float everywhere."""
    for intnum in (1, 2, 4):
        await IntFields.objects.create(intnum=intnum)

    summed = (
        await IntFields.objects.all()
        .values("intnum_null")
        .annotate(total=Sum(Case(When(intnum__gte=2, then=1), default=0)))
        .values("total")
    )
    averaged = (
        await IntFields.objects.all()
        .values("intnum_null")
        .annotate(mean=Avg(Case(When(intnum__gte=2, then=1), default=0)))
        .values("mean")
    )

    assert summed == [{"total": 2}]
    assert type(summed[0]["total"]) is int
    assert type(averaged[0]["mean"]) is float
    assert averaged[0]["mean"] == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_aggregate_over_single_column_values_subquery_returns_the_same_type_on_every_backend(db):
    """Same leak as the integer-literal Case above, for a Subquery wrapping a one-column
    values() query - its own selected column's field was never propagated, so Sum/Avg over it
    returned int/float on SQLite and Decimal on Postgres."""
    tournament = await Tournament.objects.create(name="t")
    await Event.objects.create(name="e1", tournament=tournament)
    await IntFields.objects.create(intnum=1)

    summed = (
        await IntFields.objects.all()
        .annotate(total=Sum(Subquery(Event.objects.all().annotate(events=Count("event_id")).values("events"))))
        .values("total")
    )

    assert type(summed[0]["total"]) is int


@pytest.mark.asyncio
async def test_case_annotations_differing_only_in_literal_type_do_not_share_a_cached_output_field(db):
    """A Case of plain int branches infers an integer output field, cached with the query shape -
    a later Case with the same structure and annotation name but float branches used to hit that
    cached entry and get decoded as an int (1.5 -> 1)."""
    await IntFields.objects.create(intnum=9)

    as_int = (
        await IntFields.objects.all()
        .annotate(category=Case(When(intnum__gte=8, then=1), default=0))
        .values("category")
    )
    as_float = (
        await IntFields.objects.all()
        .annotate(category=Case(When(intnum__gte=8, then=1.5), default=0.5))
        .values("category")
    )

    assert as_int == [{"category": 1}]
    assert as_float == [{"category": 1.5}]


@pytest.mark.asyncio
async def test_count_of_a_queryset_annotated_with_an_aggregate_over_a_reverse_relation_counts_groups(db):
    """CountQuery built a plain COUNT(*) over the JOIN, so a parent with two related rows was
    counted twice - count() returned 4 for a queryset that actually yields 3 rows."""
    busy = await Tournament.objects.create(name="busy")
    await Tournament.objects.create(name="idle 1")
    await Tournament.objects.create(name="idle 2")
    await Event.objects.create(name="e1", tournament=busy)
    await Event.objects.create(name="e2", tournament=busy)

    queryset = Tournament.objects.all().annotate(events_count=Count("events")).order_by("name")

    assert len(await queryset) == 3
    assert await queryset.count() == 3


@pytest.mark.asyncio
async def test_count_with_an_aggregate_annotation_matches_len_for_filters_distinct_and_m2m(db):
    busy = await Tournament.objects.create(name="busy")
    quiet = await Tournament.objects.create(name="quiet")
    first = await Event.objects.create(name="first", tournament=busy)
    second = await Event.objects.create(name="second", tournament=busy)
    await Event.objects.create(name="third", tournament=quiet)
    team_one = await Team.objects.create(name="one")
    team_two = await Team.objects.create(name="two")
    await first.participants.add(team_one, team_two)
    await second.participants.add(team_one)

    filtered_related = Tournament.objects.filter(events__name__in=["first", "second"]).annotate(n=Count("events"))
    assert await filtered_related.count() == len(await filtered_related)

    distinct = Tournament.objects.all().annotate(n=Count("events")).distinct()
    assert await distinct.count() == len(await distinct) == 2

    through_m2m = Team.objects.all().annotate(n=Count("events"))
    assert await through_m2m.count() == len(await through_m2m) == 2

    grouped_filter = Tournament.objects.all().annotate(n=Count("events")).filter(n__gte=2)
    assert await grouped_filter.count() == len(await grouped_filter) == 1

    limited = Tournament.objects.all().annotate(n=Count("events")).order_by("name").limit(1)
    assert await limited.count() == len(await limited) == 1

    no_matches = Tournament.objects.filter(name="absent").annotate(n=Count("events"))
    assert await no_matches.count() == 0


async def create_aggregation_shops() -> None:
    """Shop A: 3 products (10.50 x2, 20.00 x3, 5.25 x1) and 2 orders (100, 50); shop B: 1 product
    (7.00 x4) and 1 order (30); shop C: empty. Reviews: p1 x2, p2 x1, p4 x3."""
    shop_a = await AggregationShop.objects.create(name="A", rank=1)
    shop_b = await AggregationShop.objects.create(name="B", rank=2)
    await AggregationShop.objects.create(name="C", rank=3)
    product_one = await AggregationProduct.objects.create(shop=shop_a, name="p1", price=Decimal("10.50"), qty=2)
    product_two = await AggregationProduct.objects.create(shop=shop_a, name="p2", price=Decimal("20.00"), qty=3)
    await AggregationProduct.objects.create(shop=shop_a, name="p3", price=Decimal("5.25"), qty=1)
    product_four = await AggregationProduct.objects.create(shop=shop_b, name="p4", price=Decimal("7.00"), qty=4)
    await AggregationOrder.objects.create(shop=shop_a, total=Decimal("100.00"))
    await AggregationOrder.objects.create(shop=shop_a, total=Decimal("50.00"))
    await AggregationOrder.objects.create(shop=shop_b, total=Decimal("30.00"))
    for product, stars in (
        (product_one, 5),
        (product_one, 4),
        (product_two, 3),
        (product_four, 1),
        (product_four, 2),
        (product_four, 5),
    ):
        await AggregationReview.objects.create(product=product, stars=stars)


@pytest.mark.asyncio
async def test_aggregate_alias_matching_field_name_resolves_the_real_field(db):
    """aggregate(price=Sum("price")) used to recurse forever - the argument name found the
    annotation being resolved (itself) instead of the model field of the same name."""
    await create_aggregation_shops()

    assert await AggregationProduct.objects.all().aggregate(price=Sum("price")) == {"price": Decimal("42.75")}
    assert await AggregationOrder.objects.all().aggregate(total=Sum("total")) == {"total": Decimal("180.00")}


@pytest.mark.asyncio
async def test_annotation_named_like_a_field_or_relation_raises(db):
    with pytest.raises(FieldError, match="conflict with field"):
        AggregationShop.objects.all().annotate(name=Lower("name"))
    with pytest.raises(FieldError, match="conflict with field"):
        AggregationShop.objects.all().annotate(products=Count("products"))


@pytest.mark.asyncio
async def test_annotation_named_like_a_field_raises_before_filter_or_order_by(db):
    with pytest.raises(FieldError, match="conflict with field"):
        AggregationProduct.objects.all().annotate(price=Sum("price")).filter(price__gt=6)
    with pytest.raises(FieldError, match="conflict with field"):
        AggregationProduct.objects.all().annotate(qty=Sum("qty")).order_by("-qty")


@pytest.mark.asyncio
async def test_circular_aggregate_argument_raises_configuration_error(db):
    """A real cycle (no model field of that name to fall back to) used to end in a bare
    RecursionError."""
    await create_aggregation_shops()

    with pytest.raises(QueryError, match="Circular annotation reference"):
        await AggregationShop.objects.all().annotate(a=Count("b"), b=Count("a"))
    with pytest.raises(QueryError, match="Circular annotation reference"):
        await AggregationShop.objects.all().annotate(cycle=Count("cycle")).values_list("cycle", flat=True)


@pytest.mark.asyncio
async def test_aggregate_argument_naming_an_earlier_annotation_still_uses_it(db):
    """The lookup that made Function() prefer an annotation over a field must keep working when
    the name is a DIFFERENT, earlier annotation."""
    await create_aggregation_shops()

    rows = (
        await AggregationShop.objects.all()
        .annotate(shop_rank=F("rank") + 1)
        .annotate(total_rank=Sum("shop_rank"))
        .order_by("name")
        .values_list("name", "total_rank")
    )
    assert rows == [("A", 2), ("B", 3), ("C", 4)]


FAN_OUT_MESSAGE = "more than one to-many relation"


def fan_out_shops():
    return AggregationShop.objects.all().annotate(
        sum_of_prices=Sum("products__price"),
        products_count=Count("products", distinct=True),
        orders_count=Count("orders", distinct=True),
    )


@pytest.mark.asyncio
async def test_fan_out_guard_ignores_a_later_distinct_aggregate_over_the_same_relation(db):
    """A later Count(distinct=True) over "products" used to overwrite the flag of the earlier
    non-distinct Sum("products__price") - the guard then stayed silent for the full key set and
    the sum was silently doubled (71.50 instead of 35.75), yet raised for a narrower one."""
    await create_aggregation_shops()

    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await fan_out_shops().order_by("name").values_list("name", "sum_of_prices", "products_count", "orders_count")
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await fan_out_shops().order_by("name").values_list("name", "sum_of_prices")
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await fan_out_shops().order_by("name")
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await AggregationShop.objects.all().aggregate(
            sum_of_prices=Sum("products__price"),
            products_count=Count("products", distinct=True),
            orders_count=Count("orders", distinct=True),
        )
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await (
            AggregationShop.objects.all()
            .annotate(
                order_totals=Sum("orders__total"),
                products_count=Count("products", distinct=True),
                orders_count=Count("orders", distinct=True),
            )
            .order_by("name")
            .values_list("name", "order_totals")
        )


@pytest.mark.asyncio
async def test_fan_out_guard_does_not_depend_on_aggregate_order(db):
    await create_aggregation_shops()

    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await (
            AggregationShop.objects.all()
            .annotate(
                products_count=Count("products", distinct=True),
                sum_of_prices=Sum("products__price"),
                orders_count=Count("orders", distinct=True),
            )
            .values_list("name", "sum_of_prices")
        )


@pytest.mark.asyncio
async def test_distinct_aggregates_over_several_relations_still_work(db):
    await create_aggregation_shops()

    rows = (
        await AggregationShop.objects.all()
        .annotate(products_count=Count("products", distinct=True), orders_count=Count("orders", distinct=True))
        .order_by("name")
        .values_list("name", "products_count", "orders_count")
    )
    assert rows == [("A", 3, 2), ("B", 1, 1), ("C", 0, 0)]


@pytest.mark.asyncio
async def test_aggregates_over_one_relation_do_not_trip_the_guard(db):
    """Several aggregates (with or without distinct) over the SAME relation, and a single
    aggregate over a multi-hop chain, are correct on their own."""
    await create_aggregation_shops()

    same_relation = (
        await AggregationShop.objects.all()
        .annotate(sum_of_prices=Sum("products__price"), products_count=Count("products"))
        .order_by("name")
        .values_list("name", "sum_of_prices", "products_count")
    )
    assert same_relation == [("A", Decimal("35.75"), 3), ("B", Decimal("7.00"), 1), ("C", None, 0)]

    mixed_distinct = (
        await AggregationShop.objects.all()
        .annotate(sum_of_prices=Sum("products__price"), products_count=Count("products", distinct=True))
        .order_by("name")
        .values_list("name", "sum_of_prices", "products_count")
    )
    assert mixed_distinct == same_relation

    review_counts = (
        await AggregationShop.objects.all()
        .annotate(reviews_count=Count("products__reviews"))
        .order_by("name")
        .values_list("name", "reviews_count")
    )
    assert review_counts == [("A", 3), ("B", 3), ("C", 0)]

    one_chain = (
        await AggregationShop.objects.all()
        .annotate(reviews_count=Count("products__reviews"), stars=Sum("products__reviews__stars"))
        .order_by("name")
        .values_list("name", "reviews_count", "stars")
    )
    assert one_chain == [("A", 3, 12), ("B", 3, 8), ("C", 0, None)]


@pytest.mark.asyncio
async def test_aggregate_filter_over_another_relation_raises(db):
    """Count("products", _filter=Q(orders__total__gt=0)) joins orders too - shop A's 3 products
    used to be counted once per order (6)."""
    await create_aggregation_shops()

    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await AggregationShop.objects.all().annotate(products_count=Count("products", _filter=Q(orders__total__gt=0)))
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await AggregationShop.objects.all().annotate(
            products_count=Count("products", _filter=Q(products__reviews__stars__gt=3))
        )

    distinct_rows = (
        await AggregationShop.objects.all()
        .annotate(products_count=Count("products", distinct=True, _filter=Q(orders__total__gt=0)))
        .order_by("name")
        .values_list("name", "products_count")
    )
    assert distinct_rows == [("A", 3), ("B", 1), ("C", 0)]


@pytest.mark.asyncio
async def test_aggregate_filter_over_the_same_relation_or_negated_is_fine(db):
    await create_aggregation_shops()

    same_relation = (
        await AggregationShop.objects.all()
        .annotate(products_count=Count("products", _filter=Q(products__price__gt=6)))
        .order_by("name")
        .values_list("name", "products_count")
    )
    assert same_relation == [("A", 2), ("B", 1), ("C", 0)]

    # A negated condition over a relation becomes a correlated NOT EXISTS - no JOIN, no fan-out.
    negated = (
        await AggregationShop.objects.all()
        .annotate(products_count=Count("products", _filter=~Q(orders__total__gt=60)))
        .order_by("name")
        .values_list("name", "products_count")
    )
    assert negated == [("A", 0), ("B", 1), ("C", 0)]


@pytest.mark.asyncio
async def test_aggregates_over_a_relation_and_one_of_its_deeper_hops_raise(db):
    """Count("products__reviews") joins reviews below products - a Sum over products alone used
    to be multiplied by each product's review count (A: 46.25 instead of 35.75)."""
    await create_aggregation_shops()

    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await AggregationShop.objects.all().annotate(
            sum_of_prices=Sum("products__price"), reviews_count=Count("products__reviews")
        )


@pytest.mark.asyncio
async def test_aggregate_expression_over_two_relations_raises(db):
    """The relations crossed through F()/expressions count as well, not only a plain name."""
    await create_aggregation_shops()

    squared_prices = (
        await AggregationShop.objects.all()
        .annotate(squared=Sum(F("products__price") * F("products__price")))
        .order_by("name")
        .values_list("name", "squared")
    )
    assert squared_prices == [("A", Decimal("537.8125")), ("B", Decimal("49.0000")), ("C", None)]

    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await AggregationShop.objects.all().annotate(
            squared=Sum(F("products__price") * F("products__price")), orders_count=Count("orders")
        )
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await AggregationShop.objects.all().annotate(mixed=Sum(F("products__price") * F("orders__total")))


@pytest.mark.asyncio
async def test_filter_over_another_relation_after_or_before_annotate_raises(db):
    """annotate(Count("products")).filter(orders__total__gt=5) joins orders, repeating every
    product row per order (A: 6 instead of 3)."""
    await create_aggregation_shops()

    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await AggregationShop.objects.all().annotate(products_count=Count("products")).filter(orders__total__gt=5)
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await AggregationShop.objects.all().filter(orders__total__gt=5).annotate(products_count=Count("products"))
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await AggregationShop.objects.all().annotate(products_count=Count("products")).filter(orders__isnull=False)
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await (
            AggregationShop.objects.all().annotate(products_count=Count("products")).filter(products__reviews__stars=5)
        )
    with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
        await (
            AggregationShop.objects.all()
            .annotate(products_count=Count("products"))
            .filter(orders__total__gt=5)
            .count()
        )


@pytest.mark.asyncio
async def test_filter_over_another_relation_is_fine_for_distinct_or_negated_filters(db):
    await create_aggregation_shops()

    distinct_rows = (
        await AggregationShop.objects.all()
        .annotate(products_count=Count("products", distinct=True), orders_count=Count("orders", distinct=True))
        .filter(orders__total__gt=5)
        .order_by("name")
        .values_list("name", "products_count", "orders_count")
    )
    assert distinct_rows == [("A", 3, 2), ("B", 1, 1)]

    excluded_rows = (
        await AggregationShop.objects.all()
        .annotate(products_count=Count("products"))
        .exclude(orders__total__gt=60)
        .order_by("name")
        .values_list("name", "products_count")
    )
    assert excluded_rows == [("B", 1), ("C", 0)]


@pytest.mark.asyncio
async def test_filter_over_the_aggregated_relation_constrains_the_aggregate_in_either_order(db):
    """The current, deliberate semantics: annotate(Count("rel")).filter(rel__x=...) reuses the
    aggregate's own JOIN, so the count covers only the rows matching the filter - the same as
    filter().annotate(), unlike Django, where the call order changes the result."""
    await create_aggregation_shops()

    # Two floors on the same query shape: a repeated call must not reuse the first one's value.
    for price_floor, expected_rows in ((Decimal("6"), [("A", 2), ("B", 1)]), (Decimal("15"), [("A", 1)])):
        annotate_first = (
            await AggregationShop.objects.all()
            .annotate(products_count=Count("products"))
            .filter(products__price__gt=price_floor)
            .order_by("name")
            .values_list("name", "products_count")
        )
        filter_first = (
            await AggregationShop.objects.all()
            .filter(products__price__gt=price_floor)
            .annotate(products_count=Count("products"))
            .order_by("name")
            .values_list("name", "products_count")
        )
        assert annotate_first == filter_first == expected_rows


@pytest.mark.asyncio
async def test_exists_ignores_the_fan_out_guard(db):
    await create_aggregation_shops()

    assert (
        await AggregationShop.objects.all()
        .annotate(products_count=Count("products"), orders_count=Count("orders"))
        .filter(name="A")
        .exists()
    ) is True


@pytest.mark.asyncio
async def test_annotation_named_pk_is_rejected_for_full_model_fetch(db):
    """annotate(pk=...) used to be hydrated through the pk property, replacing each instance's
    primary key with the annotation value ([2, 1, 0, 3] instead of [1, 2, 3, 4]) so a later
    save() rewrote another row."""
    await create_aggregation_shops()

    with pytest.raises(FieldError, match="pk"):
        await AggregationProduct.objects.all().annotate(pk=Count("reviews")).order_by("id")

    expected_pks = await AggregationProduct.objects.all().order_by("id").values_list("id", flat=True)
    products = await AggregationProduct.objects.all().order_by("id")
    assert [product.pk for product in products] == expected_pks


@pytest.mark.asyncio
async def test_annotation_named_pk_raises(db):
    with pytest.raises(FieldError, match="conflict with field"):
        AggregationProduct.objects.all().annotate(pk=Count("reviews"))


@pytest.mark.asyncio
async def test_sum_of_int_field_times_float_literal_keeps_the_fraction(db):
    """Sum(F("intnum") * 1.5) used to take the IntField result type from its first operand and
    truncate the fractional result (1.5 -> 1) when decoding it."""
    await IntFields.objects.create(intnum=1)
    result = await IntFields.objects.all().aggregate(total=Sum(F("intnum") * 1.5))
    assert result["total"] == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_aggregates_over_int_field_times_int_literal_stay_integers(db):
    await IntFields.objects.create(intnum=2)
    await IntFields.objects.create(intnum=3)
    result = await IntFields.objects.all().aggregate(total=Sum(F("intnum") * 2), biggest=Max(F("intnum") * 2))
    assert result == {"total": 10, "biggest": 6}
    assert isinstance(result["total"], int)
    assert isinstance(result["biggest"], int)


@pytest.mark.asyncio
async def test_sum_of_decimal_field_times_float_literal_is_not_requantized(db):
    """Decoding the float result through the DecimalField requantized 2.625 to Decimal("2.62")."""
    await DecimalFields.objects.create(decimal=Decimal("5.25"), decimal_nodec=Decimal("0"))
    result = await DecimalFields.objects.all().aggregate(total=Sum(F("decimal") * 0.5))
    assert result["total"] == pytest.approx(2.625)


@pytest.mark.asyncio
async def test_sum_of_decimal_field_times_decimal_literal_is_still_a_decimal(db):
    await DecimalFields.objects.create(decimal=Decimal("5.25"), decimal_nodec=Decimal("0"))
    result = await DecimalFields.objects.all().aggregate(total=Sum(F("decimal") * Decimal("2")))
    assert result["total"] == Decimal("10.5000")
    assert isinstance(result["total"], Decimal)


@pytest.mark.asyncio
async def test_repeated_aggregate_over_arithmetic_with_a_different_literal_type(db):
    """The cached query shape must not carry a decoded output field over to a same-shaped
    expression whose literal has a different type."""
    await IntFields.objects.create(intnum=3)
    for literal, expected in ((2, 6), (1.5, 4.5), (2, 6), (0.5, 1.5)):
        result = await IntFields.objects.all().aggregate(total=Sum(F("intnum") * literal))
        assert result["total"] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_avg_and_max_over_arithmetic_with_a_float_literal_keep_the_fraction(db):
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=3)
    result = await IntFields.objects.all().aggregate(mean=Avg(F("intnum") * 1.5), biggest=Max(F("intnum") * 1.5))
    assert result["mean"] == pytest.approx(3.0)
    assert result["biggest"] == pytest.approx(4.5)


@pytest.mark.asyncio
async def test_decimal_filter_on_sum_annotation_compares_numerically(db):
    """A Decimal filter value against an aggregate result (which has no column affinity on
    SQLite) used to bind as TEXT, and SQLite orders every TEXT above every number - so `>` matched
    nothing and `=`/`range`/`in` never matched."""
    await create_aggregation_shops()
    sums = AggregationShop.objects.all().annotate(total=Sum("products__price"))

    async def names_matching(filtered_queryset) -> list[str]:
        return sorted([shop.name async for shop in filtered_queryset])

    assert await names_matching(sums.filter(total__gt=Decimal("10"))) == ["A"]
    assert await names_matching(sums.filter(total__gte=Decimal("35.75"))) == ["A"]
    assert await names_matching(sums.filter(total__lt=Decimal("10"))) == ["B"]
    assert await names_matching(sums.filter(total=Decimal("7.00"))) == ["B"]
    assert await names_matching(sums.filter(total__range=(Decimal("5"), Decimal("10")))) == ["B"]
    assert await names_matching(sums.filter(total__in=[Decimal("7.00")])) == ["B"]
    assert await names_matching(sums.filter(total__not=Decimal("7.00"))) == ["A", "C"]


@pytest.mark.asyncio
async def test_decimal_filter_on_annotation_repeated_with_other_value_uses_the_new_value(db):
    """The same annotation filter re-run with a different Decimal value must not reuse the
    previous value through the query shape cache."""
    await create_aggregation_shops()

    for threshold, expected_names in ((Decimal("10"), ["A"]), (Decimal("5"), ["A", "B"]), (Decimal("40"), [])):
        shops = await AggregationShop.objects.all().annotate(total=Sum("products__price")).filter(total__gt=threshold)
        assert sorted(shop.name for shop in shops) == expected_names


@pytest.mark.asyncio
async def test_decimal_filter_on_avg_count_and_int_sum_annotations(db):
    await create_aggregation_shops()

    average_shops = (
        AggregationShop.objects.all().annotate(average=Avg("products__price")).filter(average__gt=Decimal("8"))
    )
    assert sorted(shop.name for shop in await average_shops) == ["A"]

    count_shops = (
        AggregationShop.objects.all()
        .annotate(products_count=Count("products"))
        .filter(products_count__gt=Decimal("1"))
    )
    assert sorted(shop.name for shop in await count_shops) == ["A"]

    quantity_shops = (
        AggregationShop.objects.all().annotate(quantity=Sum("products__qty")).filter(quantity__gt=Decimal("5"))
    )
    assert sorted(shop.name for shop in await quantity_shops) == ["A"]


@pytest.mark.asyncio
async def test_decimal_filter_on_arithmetic_and_coalesce_annotations(db):
    await create_aggregation_shops()

    bumped_products = AggregationProduct.objects.all().annotate(bumped=F("price") + 1).filter(bumped__gt=Decimal("8"))
    assert sorted(product.name for product in await bumped_products) == ["p1", "p2"]

    scaled_products = (
        AggregationProduct.objects.all().annotate(scaled=F("price") * Decimal("2")).filter(scaled__gt=Decimal("20"))
    )
    assert sorted(product.name for product in await scaled_products) == ["p1", "p2"]

    defaulted_products = (
        AggregationProduct.objects.all()
        .annotate(defaulted=Coalesce("weight", F("price")))
        .filter(defaulted__gt=Decimal("6"))
    )
    assert sorted(product.name for product in await defaulted_products) == ["p1", "p2", "p4"]


@pytest.mark.asyncio
async def test_decimal_filter_on_subquery_annotation(db):
    await create_aggregation_shops()
    order_totals = (
        AggregationOrder.objects.filter(shop_id=OuterReference("id"))
        .values("shop_id")
        .annotate(order_sum=Sum("total"))
        .values("order_sum")
    )

    shops = (
        AggregationShop.objects.all().annotate(order_sum=Subquery(order_totals)).filter(order_sum__gt=Decimal("60"))
    )

    assert sorted(shop.name for shop in await shops) == ["A"]


@pytest.mark.asyncio
async def test_decimal_literal_inside_case_and_coalesce_aggregates(db):
    """Max(Case(... default=Value(Decimal("0")))) / Max(Coalesce("col", Decimal("0"))) compared the
    Decimal literal as TEXT against the numeric branch, so the literal always won on SQLite."""
    await create_aggregation_shops()
    await DecimalFields.objects.create(decimal=Decimal("1234567.1234"), decimal_nodec=0, decimal_null=None)
    await DecimalFields.objects.create(decimal=Decimal("1"), decimal_nodec=0, decimal_null=Decimal("5.5"))

    case_maximum = await AggregationProduct.objects.all().aggregate(
        top=Max(Case(When(qty__gt=1, then=F("price")), default=Value(Decimal("0"))))
    )
    assert case_maximum == {"top": Decimal("20.00")}

    coalesce_maximum = await DecimalFields.objects.all().aggregate(top=Max(Coalesce("decimal_null", Decimal("0"))))
    assert coalesce_maximum == {"top": Decimal("5.5000")}

    wide_maximum = await DecimalFields.objects.all().aggregate(top=Max(Coalesce("decimal", Decimal("0"))))
    assert wide_maximum == {"top": Decimal("1234567.1234")}


@pytest.mark.asyncio
async def test_order_by_case_and_coalesce_with_decimal_literal(db):
    await create_aggregation_shops()
    await AggregationProduct.objects.filter(name="p1").update(score=3)
    await AggregationProduct.objects.filter(name="p2").update(score=1)

    case_ordered = (
        await AggregationProduct.objects.all()
        .annotate(ranked=Case(When(qty__gt=2, then=F("price")), default=Value(Decimal("1"))))
        .order_by("-ranked", "name")
        .values_list("name", flat=True)
    )
    assert case_ordered == ["p2", "p4", "p1", "p3"]

    coalesce_ordered = (
        await AggregationProduct.objects.all()
        .annotate(ranked=Coalesce("score", Decimal("2.5")))
        .order_by("-ranked", "name")
        .values_list("name", flat=True)
    )
    assert coalesce_ordered == ["p1", "p3", "p4", "p2"]


@pytest.mark.asyncio
async def test_coalesce_value_decimal_default_compares_numerically(db):
    await DecimalFields.objects.create(decimal=Decimal("1"), decimal_nodec=0, decimal_null=None)
    await DecimalFields.objects.create(decimal=Decimal("1"), decimal_nodec=0, decimal_null=Decimal("5.5"))

    maximum = await DecimalFields.objects.all().aggregate(top=Max(Coalesce("decimal_null", Value(Decimal("0")))))
    minimum = await DecimalFields.objects.all().aggregate(bottom=Min(Coalesce("decimal_null", Value(Decimal("9")))))

    assert maximum == {"top": Decimal("5.5000")}
    assert minimum == {"bottom": Decimal("5.5000")}


@pytest.mark.asyncio
async def test_decimal_column_write_and_read_keep_the_exact_text_representation(db):
    """A Decimal written to a DecimalField column is still bound as its exact text, not cast: only
    Decimal literals compared outside a column are cast to NUMERIC."""
    shop = await AggregationShop.objects.create(name="X")
    product = await AggregationProduct.objects.create(shop=shop, name="w", price=Decimal("12345678.90"), qty=1)
    await AggregationProduct.objects.filter(pk=product.pk).update(price=Decimal("87654321.10"))

    assert (await AggregationProduct.objects.get(pk=product.pk)).price == Decimal("87654321.10")
    assert await AggregationProduct.objects.filter(price=Decimal("87654321.10")).count() == 1
    assert await AggregationProduct.objects.all().aggregate(top=Max("price")) == {"top": Decimal("87654321.10")}


async def _create_tournaments_with_events_and_participants():
    """Tournament "A" holds events p1 (alias 2, 2 participants), p2 (alias 3, 1 participant) and
    p3 (alias 1, none); tournament "B" holds p4 (alias 4, 3 participants); "C" is empty."""
    tournament_a = await Tournament.objects.create(name="A")
    tournament_b = await Tournament.objects.create(name="B")
    await Tournament.objects.create(name="C")
    teams = [await Team.objects.create(name=f"team {index}") for index in range(3)]
    event_p1 = await Event.objects.create(name="p1", tournament=tournament_a, alias=2)
    event_p2 = await Event.objects.create(name="p2", tournament=tournament_a, alias=3)
    await Event.objects.create(name="p3", tournament=tournament_a, alias=1)
    event_p4 = await Event.objects.create(name="p4", tournament=tournament_b, alias=4)
    await event_p1.participants.add(teams[0], teams[1])
    await event_p2.participants.add(teams[0])
    await event_p4.participants.add(teams[0], teams[1], teams[2])


@pytest.mark.asyncio
async def test_aggregate_after_a_non_aggregate_annotation_selects_only_the_metrics(db):
    """A prior non-aggregate annotation used to stay in the SELECT list next to the bare
    aggregate - Postgres rejected it ("must appear in the GROUP BY clause"), SQLite tolerated it."""
    await _create_tournaments_with_events_and_participants()

    doubled = Event.objects.all().annotate(line=F("alias") * 2)
    assert await doubled.aggregate(total=Sum("alias")) == {"total": 10}
    assert await doubled.aggregate(total=Sum("line")) == {"total": 20}
    assert await doubled.filter(line__gt=3).aggregate(total=Sum("alias")) == {"total": 9}


@pytest.mark.asyncio
async def test_aggregate_ignores_an_unreferenced_aggregate_annotation(db):
    """The JOIN of an annotation nothing refers to used to stay in the query and multiply the
    rows the metrics were computed over (7 joined rows instead of 4 events)."""
    await _create_tournaments_with_events_and_participants()

    annotated = Event.objects.all().annotate(participants_count=Count("participants"))
    assert await annotated.aggregate(total=Sum("alias"), rows=Count("event_id")) == {"total": 10, "rows": 4}

    aliased = Event.objects.all().alias(participants_count=Count("participants"))
    assert await aliased.aggregate(rows=Count("event_id")) == {"rows": 4}


@pytest.mark.asyncio
async def test_aggregate_respects_a_having_filter_on_an_aggregate_annotation(db):
    """HAVING on a query without GROUP BY doesn't filter anything - the rows are first reduced to
    one per base row in a grouped derived table, so the filter applies exactly as in count()."""
    await _create_tournaments_with_events_and_participants()

    busy = Event.objects.all().annotate(participants_count=Count("participants")).filter(participants_count__gt=1)
    assert await busy.count() == 2
    assert await busy.aggregate(rows=Count("event_id"), total=Sum("alias")) == {"rows": 2, "total": 6}
    assert await busy.aggregate(participant_rows=Count("participants")) == {"participant_rows": 5}

    either = (
        Event.objects.all()
        .annotate(participants_count=Count("participants"))
        .filter(Q(participants_count__gt=2) | Q(name="p1"))
    )
    assert await either.aggregate(rows=Count("event_id")) == {"rows": 2}

    narrowed = (
        Event.objects.filter(name__in=["p1", "p2", "p3"])
        .annotate(participants_count=Count("participants"))
        .filter(participants_count__gte=1)
    )
    assert await narrowed.aggregate(rows=Count("event_id"), most=Max("participants_count")) == {"rows": 2, "most": 2}


@pytest.mark.asyncio
async def test_aggregate_over_an_aggregate_annotation(db):
    """Max("participants_count") used to nest COUNT inside MAX - a raw database error on both
    dialects. The annotation is now computed per row in a derived table and aggregated over."""
    await _create_tournaments_with_events_and_participants()

    annotated = Event.objects.all().annotate(participants_count=Count("participants"))
    assert await annotated.aggregate(
        most=Max("participants_count"), least=Min("participants_count"), total=Sum("participants_count")
    ) == {"most": 3, "least": 0, "total": 6}

    via_alias = Event.objects.all().alias(participants_count=Count("participants"))
    assert await via_alias.aggregate(most=Max("participants_count")) == {"most": 3}

    doubled = annotated.annotate(doubled=F("participants_count") * 2)
    assert await doubled.aggregate(total=Sum("doubled")) == {"total": 12}

    per_tournament = Tournament.objects.all().annotate(events_count=Count("events")).filter(events_count__gt=0)
    assert await per_tournament.aggregate(
        tournaments=Count("id"), most=Max("events_count"), total=Sum("events_count")
    ) == {"tournaments": 2, "most": 3, "total": 4}


@pytest.mark.asyncio
async def test_aggregate_over_an_aggregate_annotation_is_not_reused_across_filter_values(db):
    """Each call resolves its own filters - the second call must not reuse the first one's
    derived table (the prior-annotation shape is never served from the query shape cache)."""
    await _create_tournaments_with_events_and_participants()

    for _ in range(2):
        first = (
            await Event.objects.filter(name="p1")
            .annotate(participants_count=Count("participants"))
            .aggregate(most=Max("participants_count"))
        )
        second = (
            await Event.objects.filter(name="p4")
            .annotate(participants_count=Count("participants"))
            .aggregate(most=Max("participants_count"))
        )
        assert (first, second) == ({"most": 2}, {"most": 3})


@pytest.mark.asyncio
async def test_aggregate_over_an_aggregate_of_an_aggregate_raises_a_clear_error(db):
    """SQL can't nest aggregate functions - both Sum(<Count annotation>) and Max(Count(...)) used to
    reach the database and fail there with a raw error."""
    await _create_tournaments_with_events_and_participants()

    nested = (
        Event.objects.all()
        .annotate(participants_count=Count("participants"))
        .annotate(total=Sum("participants_count"))
    )
    with pytest.raises(QueryError, match="aggregate function over another aggregate"):
        await nested.aggregate(most=Max("total"))
    with pytest.raises(QueryError, match="aggregate function over another aggregate"):
        await Event.objects.all().aggregate(most=Max(Count("participants")))


@pytest.mark.asyncio
async def test_aggregate_on_a_distinct_queryset_counts_each_row_once(db):
    """.distinct() was silently ignored - a filter across a to-many relation duplicates the base
    rows, and the metrics were computed over the duplicates."""
    await _create_tournaments_with_events_and_participants()

    matching = Tournament.objects.filter(events__name__in=["p1", "p2"])
    with pytest.raises(QueryError, match="to-many relation"):
        await matching.aggregate(rows=Count("id"))
    assert await matching.distinct().aggregate(rows=Count("id")) == {"rows": 1}


@pytest.mark.asyncio
async def test_aggregate_on_a_distinct_queryset_with_a_composite_primary_key(db):
    from tests.testmodels import CompositePkTriple

    await CompositePkTriple.objects.create(a=1, b=1, c=1, name="one")
    await CompositePkTriple.objects.create(a=1, b=1, c=2, name="two")

    assert await CompositePkTriple.objects.all().distinct().aggregate(rows=Count("name"), total=Sum("c")) == {
        "rows": 2,
        "total": 3,
    }


@pytest.mark.asyncio
async def test_aggregate_on_a_distinct_on_queryset_raises(db):
    with pytest.raises(QueryError, match="distinct"):
        await Event.objects.all().distinct("name").aggregate(rows=Count("event_id"))


@pytest.mark.asyncio
async def test_none_aggregate_over_an_aggregate_annotation_is_empty(db):
    await _create_tournaments_with_events_and_participants()

    empty = Event.objects.none().annotate(participants_count=Count("participants")).filter(participants_count__gt=1)
    assert await empty.aggregate(rows=Count("event_id"), fallback=Coalesce(Sum("alias"), 0)) == {
        "rows": 0,
        "fallback": 0,
    }


@pytest.mark.asyncio
async def test_aggregate_with_a_literal_argument_needs_no_column(db):
    """A bare literal as the aggregate's argument is an untyped bind parameter on Postgres -
    COUNT($1) had no type to infer at all, SUM($1) matched several overloads."""
    await _create_tournaments_with_events_and_participants()

    assert await Event.objects.all().aggregate(rows=Count(Value(1)), total=Sum(Value(1))) == {"rows": 4, "total": 4}
    assert await Event.objects.all().aggregate(rows=Count(Value("x")), most=Max(Value(7))) == {"rows": 4, "most": 7}
    assert await Event.objects.all().aggregate(total=Sum(Value(1.5))) == {"total": 6.0}
    assert await Event.objects.all().aggregate(total=Sum(Value(Decimal("1.25")))) == {"total": Decimal("5.00")}
    assert await Event.objects.all().aggregate(rows=Count(Value(date(2020, 1, 1)))) == {"rows": 4}
    assert await Event.objects.all().aggregate(rows=Count(1)) == {"rows": 4}


@pytest.mark.asyncio
async def test_literal_aggregate_argument_type_is_part_of_the_cached_query_shape(db):
    """The literal's own Python type decides its Postgres cast, so Count(Value(1)) and
    Count(Value("x")) can never share one cached query."""
    await _create_tournaments_with_events_and_participants()

    for _ in range(2):
        assert await Event.objects.all().aggregate(rows=Count(Value(1))) == {"rows": 4}
        assert await Event.objects.all().aggregate(rows=Count(Value("x"))) == {"rows": 4}
        assert (
            await Event.objects.all().annotate(first=Coalesce(Value(1), 2)).values_list("first", flat=True) == [1] * 4
        )
        assert (
            await Event.objects.all().annotate(first=Coalesce(Value("a"), "b")).values_list("first", flat=True)
            == ["a"] * 4
        )


@pytest.mark.asyncio
async def test_order_by_an_unselected_column_groups_by_it_together_with_an_aggregate(db):
    """ORDER BY a column that isn't selected next to an aggregate: Postgres raised "must appear in
    the GROUP BY clause" while SQLite silently collapsed everything into one row. The column is
    now added to the implicit GROUP BY, so both dialects return one row per group."""
    await _create_tournaments_with_events_and_participants()

    counts = Tournament.objects.all().annotate(events_count=Count("events"))
    assert await counts.order_by("name").values_list("events_count", flat=True) == [3, 1, 0]
    assert await counts.order_by("-name").values_list("events_count", flat=True) == [0, 1, 3]
    assert await counts.order_by("id").values_list("name", "events_count") == [("A", 3), ("B", 1), ("C", 0)]
    assert await counts.order_by("-events_count", "name").values_list("name", "events_count") == [
        ("A", 3),
        ("B", 1),
        ("C", 0),
    ]
    assert await counts.order_by("name").values("events_count") == [
        {"events_count": 3},
        {"events_count": 1},
        {"events_count": 0},
    ]
    assert (await counts.order_by("-name").first()).name == "C"


@pytest.mark.asyncio
async def test_meta_ordering_does_not_group_an_aggregate_query(db):
    """Event has Meta.ordering = ["name"] - it stays out of an aggregate query, like Django, so the
    groups of a grouped values query aren't split per event. An aggregate annotated before
    values_list() is computed per event (like Django)."""
    await _create_tournaments_with_events_and_participants()

    assert await Event.objects.all().values("tournament_id").annotate(
        participants_count=Count("participants")
    ).order_by("tournament_id").values_list("participants_count", flat=True) == [3, 3]
    assert sorted(
        await Event.objects.all()
        .annotate(participants_count=Count("participants"))
        .values_list("participants_count", flat=True)
    ) == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_aggregate_over_count_annotation_has_count_types(db):
    """Sum()/Avg()/Max() over a Count() annotation - Postgres returns SUM(bigint)/AVG(bigint) as
    numeric, so the result used to be a Decimal there and an int/float on SQLite."""
    first = await Author.objects.create(name="first")
    second = await Author.objects.create(name="second")
    await Author.objects.create(name="none")
    for rating in (1, 2, 3):
        await Book.objects.create(name=f"first {rating}", author=first, rating=rating)
    await Book.objects.create(name="second", author=second, rating=4)

    result = await Author.objects.annotate(book_count=Count("books")).aggregate(
        total=Sum("book_count"), mean=Avg("book_count"), most=Max("book_count")
    )

    assert result == {"total": 4, "mean": pytest.approx(4 / 3), "most": 3}
    assert type(result["total"]) is int
    assert type(result["mean"]) is float
    assert type(result["most"]) is int


@pytest.mark.asyncio
async def test_count_annotation_in_arithmetic_with_float_aggregate(db):
    """A Count() operand next to a float aggregate stays a plain number - typing the count as an
    integer field must not turn it into a mixed-field-type error."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="x", author=author, rating=1.5)
    await Book.objects.create(name="y", author=author, rating=2.5)

    product = await Author.objects.annotate(score=Count("books") * Avg("books__rating")).values_list(
        "score", flat=True
    )

    assert product == [pytest.approx(4.0)]


async def _get_tournament_ids_by_name() -> dict[str, int]:
    """Creates the shared tournament/event data and returns each tournament's id by name."""
    await _create_tournaments_with_events_and_participants()
    return {tournament.name: tournament.id for tournament in await Tournament.objects.all()}


@pytest.mark.asyncio
async def test_expression_mixing_an_aggregate_with_a_column_groups_by_the_column(db):
    """Count(...) + F("id") voted non-aggregate - no GROUP BY at all (SQLite collapsed the result
    to one row, Postgres rejected it), the aggregate landed inside GROUP BY, or its filter in WHERE."""
    ids = await _get_tournament_ids_by_name()

    counted = Tournament.objects.annotate(x=Count("events") + F("id")).order_by("id")
    assert await counted.values_list("name", "x") == [("A", 3 + ids["A"]), ("B", 1 + ids["B"]), ("C", ids["C"])]
    assert [(tournament.name, tournament.x) for tournament in await counted] == [
        ("A", 3 + ids["A"]),
        ("B", 1 + ids["B"]),
        ("C", ids["C"]),
    ]
    coalesced = Tournament.objects.annotate(x=Coalesce(Sum("events__alias"), F("id"))).order_by("id")
    assert await coalesced.values_list("name", "x") == [("A", 6), ("B", 4), ("C", ids["C"])]
    chained = Tournament.objects.annotate(n=Count("events")).annotate(x=F("n") * F("id")).order_by("id")
    assert await chained.values_list("name", "x") == [("A", 3 * ids["A"]), ("B", ids["B"]), ("C", 0)]
    threshold = 3 + ids["A"]
    expected_names = sorted(name for name, count in (("A", 3), ("B", 1), ("C", 0)) if count + ids[name] >= threshold)
    filtered = Tournament.objects.annotate(x=Count("events") + F("id")).filter(x__gte=threshold).order_by("name")
    assert await filtered.values_list("name", flat=True) == expected_names
    assert await filtered.count() == len(expected_names)


@pytest.mark.asyncio
async def test_base_table_aggregate_next_to_a_to_many_join_raises(db):
    """An aggregate over the base table's own columns was skipped by the fan-out guard - another
    aggregate's or a filter's to-many JOIN silently multiplied it."""
    await _create_tournaments_with_events_and_participants()

    with pytest.raises(QueryError, match="to-many relation"):
        await Tournament.objects.annotate(total_ids=Sum("id"), events_count=Count("events"))
    with pytest.raises(QueryError, match="to-many relation"):
        await Tournament.objects.all().aggregate(rows=Count("id"), events_count=Count("events"))
    with pytest.raises(QueryError, match="to-many relation"):
        await Tournament.objects.filter(events__alias__gte=1).aggregate(total_ids=Sum("id"))
    with pytest.raises(QueryError, match="to-many relation"):
        await Event.objects.filter(participants__id__gte=0).annotate(rows=Count("event_id")).filter(rows=2)

    distinct_rows = Tournament.objects.annotate(rows=Count("id", distinct=True), events_count=Count("events"))
    assert await distinct_rows.order_by("name").values_list("name", "rows", "events_count") == [
        ("A", 1, 3),
        ("B", 1, 1),
        ("C", 1, 0),
    ]
    assert await Tournament.objects.annotate(events_count=Count("events")).aggregate(
        total=Sum("events_count"), rows=Count("id")
    ) == {"total": 4, "rows": 3}
    filtered_counts = Tournament.objects.filter(events__alias__gte=2).annotate(events_count=Count("events"))
    assert await filtered_counts.order_by("name").values_list("name", "events_count") == [("A", 2), ("B", 1)]
    assert await Tournament.objects.filter(events__alias__gte=2).distinct().aggregate(rows=Count("id")) == {"rows": 2}


@pytest.mark.asyncio
async def test_exclude_on_an_aggregate_annotation_keeps_null_groups(db):
    """exclude()/~Q() on an aggregate rendered a plain HAVING NOT (...) - an empty group's NULL
    SUM/MAX dropped out of both the filter and its negation."""
    await _create_tournaments_with_events_and_participants()

    summed = Tournament.objects.annotate(total=Sum("events__alias"))
    assert await summed.exclude(total__gt=5).order_by("name").values_list("name", flat=True) == ["B", "C"]
    highest = Tournament.objects.annotate(highest=Max("events__alias"))
    assert await highest.filter(~Q(highest=3)).order_by("name").values_list("name", flat=True) == ["B", "C"]
    assert await highest.exclude(highest=3).count() == 2


@pytest.mark.asyncio
async def test_exclude_of_an_aggregate_annotation_combined_with_a_forward_relation(db):
    """A negated condition crossing a relation went into a NOT EXISTS subquery built from its WHERE part
    alone - the aggregate's HAVING part was dropped (`&`), or the subquery had no condition at all and
    failed to render (`|`)."""
    await _create_tournaments_with_events_and_participants()

    counted = Event.objects.annotate(participants_count=Count("participants"))
    assert await counted.exclude(Q(tournament__name="A") | Q(participants_count=0)).values_list("name", flat=True) == [
        "p4"
    ]
    assert await counted.exclude(Q(tournament__name="A") & Q(participants_count__gt=1)).order_by("name").values_list(
        "name", flat=True
    ) == ["p2", "p3", "p4"]
    assert await counted.filter(~(Q(tournament__name="B") | Q(participants_count__lt=2))).values_list(
        "name", flat=True
    ) == ["p1"]
    with pytest.raises(QueryError, match="to-many relation participants"):
        await counted.exclude(Q(participants__name="team 0") | Q(participants_count=0))


@pytest.mark.asyncio
async def test_implicit_group_by_of_a_parameterized_expression(db):
    """A grouped expression with a literal was rendered again for GROUP BY with a new bind
    placeholder - Postgres no longer matched it to the selected expression."""
    await _create_tournaments_with_events_and_participants()

    shifted = Event.objects.annotate(shifted=F("alias") + 5, participants_count=Count("participants"))
    assert await shifted.order_by("shifted").values_list("shifted", "participants_count") == [
        (6, 0),
        (7, 2),
        (8, 1),
        (9, 3),
    ]
    labelled = (
        Event.objects.annotate(label=Case(When(alias__gt=2, then=Value("high")), default=Value("low")))
        .values("label")
        .annotate(participants_count=Count("participants"))
    )
    assert await labelled.order_by("label") == [
        {"label": "high", "participants_count": 4},
        {"label": "low", "participants_count": 2},
    ]
    fallback = Event.objects.annotate(bonus=Coalesce("alias", 0) * 10, participants_count=Count("participants"))
    assert await fallback.order_by("bonus").values_list("bonus", "participants_count") == [
        (10, 0),
        (20, 2),
        (30, 1),
        (40, 3),
    ]
    concatenated = Event.objects.annotate(
        full_name=Concat("tournament__name", Value("-"), F("name")), participants_count=Count("participants")
    ).order_by("event_id")
    assert [(event.full_name, event.participants_count) for event in await concatenated] == [
        ("A-p1", 2),
        ("A-p2", 1),
        ("A-p3", 0),
        ("B-p4", 3),
    ]


@pytest.mark.asyncio
async def test_count_with_a_non_aggregate_annotation_counts_rows(db):
    """count() grouped only by the non-aggregate annotation - it counted distinct annotation
    values, not rows."""
    await _create_tournaments_with_events_and_participants()

    by_tournament = Event.objects.annotate(
        tournament_name=Lower("tournament__name"), participants_count=Count("participants")
    )
    assert await by_tournament.count() == len(await by_tournament) == 4
    by_alias_parity = Event.objects.annotate(parity=F("alias") % 2, participants_count=Count("participants"))
    assert await by_alias_parity.count() == len(await by_alias_parity) == 4


@pytest.mark.asyncio
async def test_count_of_a_distinct_queryset_filtered_on_an_aggregate(db):
    """COUNT(DISTINCT pk) was computed once per group and only the first group's 1 was read."""
    await _create_tournaments_with_events_and_participants()

    with_events = Tournament.objects.annotate(events_count=Count("events")).filter(events_count__gte=1).distinct()
    assert await with_events.count() == len(await with_events) == 2


@pytest.mark.asyncio
async def test_filter_comparing_a_field_with_an_aggregate_annotation_uses_having(db):
    """filter(field__lt=F("aggregate")) put the aggregate into WHERE."""
    await _create_tournaments_with_events_and_participants()

    counted = Event.objects.annotate(participants_count=Count("participants")).order_by("name")
    assert await counted.filter(alias__lte=F("participants_count")).values_list("name", flat=True) == ["p1"]
    assert await counted.filter(participants_count__gte=F("alias")).values_list("name", flat=True) == ["p1"]
    assert await counted.exclude(alias__lte=F("participants_count")).values_list("name", flat=True) == [
        "p2",
        "p3",
        "p4",
    ]


@pytest.mark.asyncio
async def test_having_column_outside_the_aggregate_is_grouped(db):
    """A column HAVING reads next to an aggregate (an OR with a plain filter, a comparison with a
    column) was never added to the implicit GROUP BY."""
    await _create_tournaments_with_events_and_participants()

    busy_or_b = Event.objects.annotate(participants_count=Count("participants")).filter(
        Q(participants_count__gte=2) | Q(tournament__name="B")
    )
    assert await busy_or_b.order_by("name").values_list("name", flat=True) == ["p1", "p4"]
    assert [event.name for event in await busy_or_b.order_by("name")] == ["p1", "p4"]
    assert await busy_or_b.count() == 2
    assert await busy_or_b.exists() is True
    counted = Event.objects.annotate(participants_count=Count("participants")).filter(
        participants_count__gte=F("alias")
    )
    assert await counted.order_by("name").values_list("name", flat=True) == ["p1"]


@pytest.mark.asyncio
async def test_values_with_a_correlated_subquery_groups_by_its_outer_reference(db):
    """values() put the correlated subquery itself into GROUP BY instead of the outer column it
    reads, and a Case() branch's plain column was left out of it."""
    ids = await _get_tournament_ids_by_name()

    with_exists = Tournament.objects.annotate(
        events_count=Count("events"),
        has_high_alias=Exists(Event.objects.filter(tournament_id=OuterReference("id"), alias__gt=3)),
    )
    assert await with_exists.order_by("name").values_list("name", "events_count", "has_high_alias") == [
        ("A", 3, False),
        ("B", 1, True),
        ("C", 0, False),
    ]
    with_subquery = Tournament.objects.annotate(
        events_count=Count("events"),
        top_alias=Subquery(
            Event.objects.filter(tournament_id=OuterReference("id")).order_by("-alias").limit(1).values("alias")
        ),
    )
    assert await with_subquery.order_by("name").values_list("name", "events_count", "top_alias") == [
        ("A", 3, 3),
        ("B", 1, 4),
        ("C", 0, None),
    ]
    with_case = Tournament.objects.annotate(x=Case(When(name="A", then=Count("events")), default=F("id")))
    assert await with_case.order_by("name").values_list("name", "x") == [("A", 3), ("B", ids["B"]), ("C", ids["C"])]


@pytest.mark.asyncio
async def test_avg_min_max_accept_distinct(db):
    for num in (1, 1, 4):
        await ExpressionTypeParity.objects.create(
            num=num, big=num, dec=Decimal(num), flag=True, grp="a", ts=datetime(2020, 1, 1, tzinfo=UTC)
        )
    assert await ExpressionTypeParity.objects.all().aggregate(
        average=Avg("num", distinct=True),
        plain_average=Avg("num"),
        largest=Max("num", distinct=True),
        smallest=Min("num", distinct=True),
    ) == {"average": 2.5, "plain_average": 2.0, "largest": 4, "smallest": 1}
    rows = (
        await ExpressionTypeParity.objects.all()
        .annotate(c=Avg("big", distinct=True))
        .group_by("grp")
        .values_list("c", flat=True)
    )
    assert rows == [2.5]
    assert type(rows[0]) is float


@pytest.mark.asyncio
async def test_aggregation_filter_is_an_aggregate_filter_clause(db):
    """`_filter=` renders as the aggregate's own `FILTER (WHERE ...)` on every dialect - a row
    failing it never reaches the aggregate."""
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Event 1", tournament=tournament)
    await Event.objects.create(name="Event 2", tournament=tournament)

    queryset = Tournament.objects.annotate(
        matching=Count("events", _filter=Q(events__name="Event 1")),
        missing=Count("events", _filter=Q(events__name="Nothing")),
        highest=Max("events__name", _filter=Q(events__name__not="Event 2")),
        none_highest=Max("events__name", _filter=Q(events__name="Nothing")),
    ).values("matching", "missing", "highest", "none_highest")

    assert "FILTER(WHERE" in queryset.sql()
    assert await queryset == [{"matching": 1, "missing": 0, "highest": "Event 1", "none_highest": None}]


@pytest.mark.asyncio
async def test_annotate_rejects_a_value_that_is_not_an_expression(db):
    """`annotate(v="tags")` used to fail deep in query building with AttributeError."""
    with pytest.raises(TypeError, match=r"use F\('name'\) to reference a field"):
        Tournament.objects.annotate(label="name")
    with pytest.raises(TypeError, match="must be an expression"):
        Tournament.objects.all().alias(label=5)


@pytest.mark.asyncio
async def test_fan_out_guard_counts_to_many_joins_outside_aggregates(db):
    """A second to-many JOIN made by a non-aggregate annotation, a values() field or the ordering
    inflates a non-distinct aggregate just like a filter's does."""
    await create_aggregation_shops()
    shops = AggregationShop.objects.filter(name="A").annotate(products_count=Count("products"))

    inflating_querysets = (
        shops.annotate(order_total=F("orders__total")).values_list("name", "products_count"),
        shops.annotate(big_order=Case(When(orders__total__gt=60, then=Value(1)), default=Value(0))).values_list(
            "products_count", flat=True
        ),
        shops.annotate(order_total=Coalesce("orders__total", 0)).values_list("products_count", flat=True),
        shops.order_by("orders__total").values_list("products_count", flat=True),
        shops.annotate(order_total=F("orders__total")),
    )
    for queryset in inflating_querysets:
        with pytest.raises(QueryError, match=FAN_OUT_MESSAGE):
            await queryset
    # A values() field is a GROUP BY key - a non-unique one doesn't pin one order per group.
    with pytest.raises(QueryError, match="neither the relation's primary key nor a unique field"):
        await shops.values_list("name", "orders__total", "products_count")

    distinct_counts = await (
        AggregationShop.objects.filter(name="A")
        .annotate(products_count=Count("products", distinct=True), order_total=F("orders__total"))
        .order_by("order_total")
        .values_list("products_count", "order_total")
    )
    assert distinct_counts == [(3, Decimal("50.00")), (3, Decimal("100.00"))]
    same_relation = (
        await shops.annotate(product_name=F("products__name"))
        .order_by("product_name")
        .values_list("product_name", "products_count")
    )
    assert same_relation == [("p1", 1), ("p2", 1), ("p3", 1)]


@pytest.mark.asyncio
async def test_fan_out_error_names_the_single_joined_relation(db):
    await create_aggregation_shops()

    with pytest.raises(QueryError) as error:
        await (
            AggregationShop.objects.annotate(product_rows=Count("id"))
            .filter(products__qty__gt=0)
            .values_list("product_rows", flat=True)
        )
    message = str(error.value)
    assert "more than one to-many relation" not in message
    assert "over the base table rows is combined with a JOIN over the to-many relation products" in message


@pytest.mark.asyncio
async def test_aggregate_after_group_by_runs_over_the_grouped_rows(db):
    await create_aggregation_shops()
    grouped_products = AggregationProduct.objects.annotate(product_count=Count("id")).group_by("shop_id")
    assert sorted(count for _shop_id, count in await grouped_products.values_list("shop_id", "product_count")) == [
        1,
        3,
    ]

    assert await grouped_products.aggregate(most=Max("product_count"), groups=Count("shop_id")) == {
        "most": 3,
        "groups": 2,
    }
    assert await grouped_products.filter(product_count__gte=2).aggregate(total=Sum("product_count")) == {"total": 3}
    assert await AggregationProduct.objects.all().group_by("shop_id").aggregate(groups=Count("shop_id")) == {
        "groups": 2
    }
    assert await AggregationProduct.objects.annotate(total_qty=Sum("qty")).group_by("shop__rank").aggregate(
        largest=Max("total_qty"), ranks=Count("shop__rank")
    ) == {"largest": 6, "ranks": 2}
    assert await grouped_products.none().aggregate(most=Max("product_count")) == {"most": None}

    with pytest.raises(QueryError, match="isn't grouped by"):
        await grouped_products.aggregate(total_price=Sum("price"))


@pytest.mark.asyncio
async def test_aggregate_filter_accepts_an_exists_condition(db):
    await create_aggregation_shops()
    has_orders = Exists(AggregationOrder.objects.filter(shop_id=OuterReference("id")))

    assert await AggregationShop.objects.all().aggregate(with_orders=Count("id", _filter=has_orders)) == {
        "with_orders": 2
    }
    rows = (
        await AggregationShop.objects.annotate(without_orders=Count("id", _filter=~has_orders))
        .order_by("name")
        .values_list("name", "without_orders")
    )
    assert rows == [("A", 0), ("B", 0), ("C", 1)]
    with pytest.raises(TypeError, match="takes a Q or an Exists condition"):
        Count("id", _filter="orders")


async def create_tagged_products() -> None:
    """The aggregation shops, with tag "t1" on p1 and p2 and tag "t2" on p2 and p4 - p3 has none."""
    await create_aggregation_shops()
    first_tag = await AggregationTag.objects.create(name="t1")
    second_tag = await AggregationTag.objects.create(name="t2")
    products = {product.name: product for product in await AggregationProduct.objects.all()}
    await products["p1"].tags.add(first_tag)
    await products["p2"].tags.add(first_tag, second_tag)
    await products["p4"].tags.add(second_tag)


@pytest.mark.asyncio
async def test_group_by_a_to_many_field_partitions_instead_of_fanning_out(db):
    """Grouping by a to-many relation's field splits the rows by it - an aggregate over the base
    rows is summed per key, like Django, instead of being rejected as fanned out."""
    await create_tagged_products()
    expected = sorted([("t1", 5), ("t2", 7), (None, 1)], key=repr)
    products = AggregationProduct.objects.annotate(total_qty=Sum("qty"))

    assert sorted(await products.group_by("tags__name").values_list("tags__name", "total_qty"), key=repr) == expected
    assert (
        sorted(
            await AggregationProduct.objects.all()
            .values("tags__name")
            .annotate(total_qty=Sum("qty"))
            .values_list("tags__name", "total_qty"),
            key=repr,
        )
        == expected
    )
    assert sorted(
        await AggregationProduct.objects.annotate(n=Count("id")).group_by("tags__name").values("tags__name", "n"),
        key=repr,
    ) == sorted([{"tags__name": "t1", "n": 2}, {"tags__name": "t2", "n": 2}, {"tags__name": None, "n": 1}], key=repr)
    assert sorted(
        await AggregationProduct.objects.all()
        .alias(tag=F("tags__name"))
        .annotate(n=Count("id"))
        .group_by("tag")
        .values_list("tag", "n"),
        key=repr,
    ) == sorted([("t1", 2), ("t2", 2), (None, 1)], key=repr)
    assert sorted(
        await AggregationShop.objects.annotate(n=Count("id"))
        .group_by("products__name")
        .values_list("products__name", "n"),
        key=repr,
    ) == sorted([("p1", 1), ("p2", 1), ("p3", 1), ("p4", 1), (None, 1)], key=repr)


@pytest.mark.asyncio
async def test_group_by_a_to_many_field_still_rejects_another_to_many_aggregate(db):
    """The key's relation only partitions the rows when no non-distinct aggregate reads another
    to-many relation - reviews repeat once per tag row otherwise."""
    await create_tagged_products()
    with pytest.raises(QueryError, match="neither the relation's primary key nor a unique field"):
        await (
            AggregationProduct.objects.annotate(n=Count("reviews"))
            .group_by("tags__name")
            .values_list("tags__name", "n")
        )
    with pytest.raises(QueryError, match="to-many relation"):
        await (
            AggregationProduct.objects.annotate(s=Sum("qty"))
            .filter(reviews__stars__gte=1)
            .group_by("tags__name")
            .values_list("tags__name", "s")
        )


@pytest.mark.asyncio
async def test_group_by_a_to_many_primary_key_allows_another_to_many_aggregate(db):
    """A key identifying one related row pins it per group, so an aggregate over another to-many
    relation only repeats per its own rows - allowed, as in Django."""
    await create_tagged_products()
    by_tag_id = AggregationProduct.objects.filter(tags__id__isnull=False).order_by("tags__id")

    assert await by_tag_id.values("tags__id").annotate(n=Count("reviews")).values_list("n", flat=True) == [3, 4]
    assert await by_tag_id.values("tags__id", "tags__name").annotate(stars=Sum("reviews__stars")).values_list(
        "tags__name", "stars"
    ) == [("t1", 12), ("t2", 11)]
    assert await AggregationProduct.objects.annotate(n=Count("reviews")).group_by("tags__id").filter(
        tags__id__isnull=False
    ).order_by("tags__id").values_list("tags__id", "n") == [
        (tag_id, count)
        for tag_id, count in zip(
            await AggregationTag.objects.all().order_by("id").values_list("id", flat=True), [3, 4], strict=True
        )
    ]
    # The base rows still repeat per review.
    with pytest.raises(QueryError, match="over the base table rows"):
        await AggregationProduct.objects.all().values("tags__id").annotate(total_qty=Sum("qty"), n=Count("reviews"))


def test_group_key_identifies_a_related_row_only_by_its_primary_key_or_a_unique_field():
    def lookup_ends_on_unique_field(model, lookup):
        key = AggregatedMultiValuedPaths.get_last_multi_valued_key(model, lookup)
        return key is not None and AggregatedMultiValuedPaths.field_names_identify_row(*key)

    assert lookup_ends_on_unique_field(AggregationProduct, "tags")
    assert lookup_ends_on_unique_field(AggregationProduct, "tags__id")
    assert lookup_ends_on_unique_field(AggregationProduct, "tags__pk")
    assert not lookup_ends_on_unique_field(AggregationProduct, "tags__name")
    assert not lookup_ends_on_unique_field(AggregationProduct, "shop__id")
    assert not lookup_ends_on_unique_field(AggregationProduct, "reviews__product__id")
    assert not lookup_ends_on_unique_field(AggregationProduct, "name")


@pytest.mark.asyncio
async def test_group_by_a_to_many_field_rejects_a_separate_filter_join_of_that_relation(db):
    """A second .filter() call on the key's relation builds its own JOIN, which repeats the rows
    inside each group - only the key's own JOIN partitions them."""
    await create_tagged_products()
    by_tag_name = AggregationProduct.objects.all().values("tags__name").annotate(total_qty=Sum("qty"))

    assert await by_tag_name.filter(tags__id__gt=0).order_by("tags__name").values_list("tags__name", "total_qty") == [
        ("t1", 5),
        ("t2", 7),
    ]
    assert await AggregationProduct.objects.filter(tags__id__gt=0, tags__name__in=["t1", "t2"]).values(
        "tags__name"
    ).annotate(total_qty=Sum("qty")).order_by("tags__name").values_list("tags__name", "total_qty") == [
        ("t1", 5),
        ("t2", 7),
    ]
    inflating_querysets = (
        by_tag_name.filter(tags__id__gt=0).filter(tags__id__gt=0),
        AggregationProduct.objects.filter(tags__name__isnull=False)
        .values("tags__name")
        .annotate(total_qty=Sum("qty"))
        .filter(tags__name__in=["t1", "t2"]),
        AggregationProduct.objects.all()
        .values("tags__name")
        .annotate(n=Count("id"))
        .filter(tags__id__gt=0)
        .filter(tags__id__gt=0),
        AggregationProduct.objects.annotate(total_qty=Sum("qty"))
        .group_by("tags__name")
        .filter(tags__id__gt=0)
        .filter(tags__id__gt=0)
        .values_list("tags__name", "total_qty"),
    )
    for queryset in inflating_querysets:
        with pytest.raises(QueryError, match=r"tags \(the separate JOIN of another \.filter\(\) call\)"):
            await queryset


@pytest.mark.asyncio
async def test_fan_out_error_only_suggests_distinct_for_count(db):
    """distinct=True dedupes values, not repeated rows - it fixes Count() of the relation itself,
    never Sum()/Avg()."""
    await create_aggregation_shops()
    with pytest.raises(QueryError, match=r"distinct=True doesn't help Sum\(\)"):
        await (
            AggregationShop.objects.annotate(total_rank=Sum("rank"))
            .filter(orders__total__gt=1)
            .values_list("id", "total_rank")
        )
    with pytest.raises(QueryError, match=r"Pass distinct=True to Count\(\)"):
        await AggregationShop.objects.annotate(n=Count("products")).filter(orders__total__gt=1).values_list("id", "n")


@pytest.mark.asyncio
async def test_max_and_min_ignore_rows_another_to_many_join_repeats(db):
    """A repeated row can't change MAX/MIN, so another to-many JOIN doesn't need distinct=True."""
    await create_aggregation_shops()
    assert await AggregationShop.objects.annotate(top=Max("products__price"), low=Min("products__price")).filter(
        orders__total__gte=50
    ).distinct().order_by("id").values_list("name", "top", "low") == [("A", Decimal("20.00"), Decimal("5.25"))]


@pytest.mark.asyncio
async def test_outer_ref_through_a_to_many_relation_counts_as_a_fan_out_join(db):
    """OuterReference("products__id") joins products into the outer query, repeating its rows."""
    await create_aggregation_shops()
    first_stars = Subquery(
        AggregationReview.objects.filter(product=OuterReference("products__id"))
        .order_by("id")
        .limit(1)
        .values("stars")
    )
    with pytest.raises(QueryError, match="to-many relation products"):
        await AggregationShop.objects.annotate(n=Count("id"), stars=first_stars).values_list("id", "n", "stars")


@pytest.mark.asyncio
async def test_update_to_an_aggregate_is_rejected(db):
    await create_aggregation_shops()
    with pytest.raises(FieldError, match="can't be updated to an aggregate"):
        await AggregationProduct.objects.annotate(n=Count("reviews")).update(qty=F("n"))
    with pytest.raises(FieldError, match="can't be updated to an aggregate"):
        await AggregationProduct.objects.filter(name="p1").update(qty=Sum("qty"))
    with pytest.raises(FieldError, match="can't be updated to an aggregate"):
        await AggregationProduct.objects.annotate(n=Count("reviews")).filter(n__gte=1).update(qty=F("n") + 1)

    review_count = Subquery(
        AggregationReview.objects.filter(product=OuterReference("pk"))
        .group_by("product_id")
        .annotate(n=Count("id"))
        .values("n")
    )
    assert await AggregationProduct.objects.filter(name__in=["p1", "p4"]).update(qty=review_count) == 2
    assert await AggregationProduct.objects.all().order_by("name").values_list("name", "qty") == [
        ("p1", 2),
        ("p2", 3),
        ("p3", 1),
        ("p4", 3),
    ]


@pytest.mark.asyncio
async def test_group_by_an_aggregate_annotation_is_rejected(db):
    await create_aggregation_shops()
    with pytest.raises(FieldError, match="Cannot group by 'n'"):
        await AggregationProduct.objects.annotate(n=Count("reviews")).group_by("n").values_list("n", flat=True)


@pytest.mark.asyncio
@test.requires_features(supports_select_for_update=True)
async def test_select_for_update_with_an_aggregate_is_rejected(db):
    await create_aggregation_shops()
    async with Transactions.atomic():
        with pytest.raises(QueryError, match=r"select_for_update\(\) can't be combined with an aggregate"):
            await AggregationProduct.objects.annotate(n=Count("reviews")).filter(n__gte=1).select_for_update()
        with pytest.raises(QueryError, match=r"select_for_update\(\) can't be combined with an aggregate"):
            await AggregationProduct.objects.annotate(n=Count("reviews")).select_for_update().values_list("id", "n")


async def _create_events_with_participants() -> tuple[Tournament, Event, Event, Team, Team, Team]:
    """Tournament "t" holds event "both" (teams x and y) and event "first" (team x); tournament "o"
    holds event "none" with no teams."""
    tournament = await Tournament.objects.create(name="t")
    other = await Tournament.objects.create(name="o")
    first_team, second_team, third_team = [await Team.objects.create(name=name) for name in ("x", "y", "z")]
    both = await Event.objects.create(name="both", tournament=tournament)
    await both.participants.add(first_team, second_team)
    only_first = await Event.objects.create(name="first", tournament=tournament)
    await only_first.participants.add(first_team)
    await Event.objects.create(name="none", tournament=other)
    return tournament, both, only_first, first_team, second_team, third_team


@pytest.mark.asyncio
async def test_to_many_relation_lookup_joins_like_a_lookup_on_its_primary_key(db):
    """A lookup on a to-many relation's own name built a JOIN of its own: two .filter() calls shared
    it (always empty), one call didn't share it with the other lookups through the relation, and the
    aggregate fan-out checks didn't see it (inflated counts)."""
    _tournament, both, only_first, first_team, second_team, third_team = await _create_events_with_participants()

    def get_event_ids(queryset):
        return queryset.order_by("event_id").values_list("event_id", flat=True)

    assert await get_event_ids(Event.objects.filter(participants=first_team).filter(participants=second_team)) == [
        both.event_id
    ]
    assert await get_event_ids(
        Event.objects.filter(participants__in=[first_team]).filter(participants__in=[second_team])
    ) == [both.event_id]
    assert await get_event_ids(Event.objects.filter(Q(participants=first_team) & Q(participants__name="y"))) == []
    assert await Event.objects.filter(participants=first_team).order_by("event_id").values_list(
        "event_id", "participants__name"
    ) == [(both.event_id, "x"), (only_first.event_id, "x")]
    assert await Event.objects.filter(participants__in=[first_team, third_team]).annotate(
        n=Count("participants")
    ).order_by("event_id").values_list("event_id", "n") == [(both.event_id, 1), (only_first.event_id, 1)]
    assert await Tournament.objects.filter(events=both).filter(events=only_first).values_list("name", flat=True) == [
        "t"
    ]
    assert await Tournament.objects.filter(Q(events=both) & Q(events__name="first")).count() == 0
    assert await Tournament.objects.filter(events__in=[both, only_first]).annotate(n=Count("events")).values_list(
        "name", "n"
    ) == [("t", 2)]
    assert await Tournament.objects.filter(events__isnull=False).values("name").annotate(n=Count("events")).order_by(
        "name"
    ) == [{"name": "o", "n": 1}, {"name": "t", "n": 2}]


@pytest.mark.asyncio
async def test_exclude_across_a_to_many_relation_takes_no_join_of_the_query(db):
    """exclude(rel__...) took the relation's JOIN, so a later filter(rel__...) built a second one -
    values()/order_by()/aggregates over the relation read the unfiltered JOIN, and the fan-out
    check rejected the aggregate."""
    _tournament, both, only_first, *_teams = await _create_events_with_participants()

    rows = Event.objects.exclude(participants__name="z").filter(participants__name="x").order_by("event_id")
    assert await rows.values_list("event_id", "participants__name") == [
        (both.event_id, "x"),
        (only_first.event_id, "x"),
    ]
    assert await Event.objects.exclude(participants__name="z").filter(participants__name__in=["x", "y"]).annotate(
        n=Count("participants")
    ).order_by("event_id").values_list("event_id", "n") == [(both.event_id, 2), (only_first.event_id, 1)]
    assert await Event.objects.filter(participants__name="x").exclude(participants__name="y").values_list(
        "event_id", "participants__name"
    ) == [(only_first.event_id, "x")]


@pytest.mark.asyncio
async def test_group_key_uniqueness_needs_non_nullable_unique_fields(db):
    """A nullable unique field was taken as identifying one related row - every NULL row shares
    one group; a full unique_together set or composite primary key wasn't."""
    identify_row = AggregatedMultiValuedPaths.field_names_identify_row
    assert identify_row(UniqueName, ["id"])
    assert not identify_row(UniqueName, ["name"])
    assert identify_row(UniqueTogetherFields, ["first_name", "last_name"])
    assert not identify_row(UniqueTogetherFields, ["first_name"])
    assert identify_row(UniqueTogetherFieldsWithFK, ["text", "tournament"])
    assert identify_row(UniqueTogetherFieldsWithFK, ["text", "tournament_id"])
    assert identify_row(CompositePkOwningFK, ["a", "b"])
    assert not identify_row(CompositePkOwningFK, ["a"])
    # A filter of the JOIN's own .filter() call can rule the NULLs out.
    assert identify_row(UniqueName, ["name"], ["name"])
    assert QueryJoins.get_non_null_filtered_lookups(
        Tournament.objects.filter(events__name__isnull=False)._get_compiler()
    ) == {"events__name"}
    assert QueryJoins.get_non_null_filtered_lookups(
        Tournament.objects.filter(events__name="x", desc=None)._get_compiler()
    ) == {"events__name"}
    assert QueryJoins.get_non_null_filtered_lookups(
        Tournament.objects.exclude(events__event_id=1).filter(events__name__in=["x"])._get_compiler()
    ) == {"events__name"}
    assert not QueryJoins.get_non_null_filtered_lookups(
        Tournament.objects.filter(events__event_id=1).filter(events__name__isnull=False)._get_compiler()
    ) - {"events__event_id"}
    assert (
        QueryJoins.get_non_null_filtered_lookups(
            Tournament.objects.filter(Q(events__name="x") | Q(name="t"))._get_compiler()
        )
        == set()
    )

    tournament, *_rest = await _create_events_with_participants()
    await CompositePkOwningFK.objects.create(a=1, b=1, name="first", tournament=tournament)
    await CompositePkOwningFK.objects.create(a=1, b=2, name="second", tournament=tournament)
    grouped = (
        Tournament.objects.filter(composite_owners__isnull=False)
        .values("composite_owners__a", "composite_owners__b")
        .annotate(n=Count("events"))
        .order_by("composite_owners__b")
    )
    assert await grouped.values_list("composite_owners__a", "composite_owners__b", "n") == [(1, 1, 2), (1, 2, 2)]


@pytest.mark.asyncio
async def test_correlated_subquery_group_key_groups_by_its_value(db):
    """A correlated Subquery used as a group key was grouped by the outer column its OuterReference reads -
    two outer rows with the same subquery value landed in separate groups."""
    first_author = await Author.objects.create(name="same")
    second_author = await Author.objects.create(name="same")
    third_author = await Author.objects.create(name="other")
    for author, count in ((first_author, 2), (second_author, 1), (third_author, 1)):
        for index in range(count):
            await Book.objects.create(name=f"{author.id}-{index}", author=author, rating=1)
    author_name = Subquery(Author.objects.filter(id=OuterReference("author_id")).values("name")[:1])

    grouped = Book.objects.all().values(author_name=author_name).annotate(n=Count("id"))
    assert await grouped.order_by("author_name") == [
        {"author_name": "other", "n": 1},
        {"author_name": "same", "n": 3},
    ]
    assert await grouped.count() == 2
    assert await grouped.filter(n__gt=1).values_list("author_name", flat=True) == ["same"]
    assert await grouped.aggregate(most=Max("n")) == {"most": 3}


@pytest.mark.asyncio
async def test_filter_after_values_annotate_reuses_the_aggregate_join(db):
    """Unlike Django (which adds a second JOIN and inflates the count), a to-many filter chained after
    values().annotate() over the same relation narrows the aggregated rows."""
    author = await Author.objects.create(name="a")
    for rating in (1, 3, 5):
        await Book.objects.create(name=f"b{rating}", author=author, rating=rating)

    assert await Author.objects.all().values("name").annotate(n=Count("books")).filter(books__rating__gte=3) == [
        {"name": "a", "n": 2}
    ]


@pytest.mark.asyncio
@test.requires_features(supports_select_for_update=True)
async def test_select_for_update_with_distinct_a_window_or_a_set_operation_is_rejected(db):
    await create_aggregation_shops()
    async with Transactions.atomic():
        with pytest.raises(QueryError, match=r"select_for_update\(\) can't be combined with \.distinct\(\)"):
            await AggregationProduct.objects.all().values_list("shop_id", flat=True).distinct().select_for_update()
        with pytest.raises(QueryError, match=r"select_for_update\(\) can't be combined with \.distinct\(\)"):
            await AggregationProduct.objects.all().distinct().select_for_update()
        with pytest.raises(QueryError, match=r"select_for_update\(\) can't be combined with a window"):
            await (
                AggregationProduct.objects.all()
                .values("id", position=Window(RowNumber(), order_by=["id"]))
                .select_for_update()
            )
        with pytest.raises(QueryError, match=r"select_for_update\(\) can't be combined with a window"):
            await (
                AggregationProduct.objects.all()
                .annotate(position=Window(RowNumber(), order_by=["id"]))
                .select_for_update()
            )
        with pytest.raises(QueryError, match="branch of union"):
            await (
                AggregationProduct.objects.filter(qty=1)
                .values_list("id", flat=True)
                .select_for_update()
                .union(AggregationProduct.objects.filter(qty=2).values_list("id", flat=True))
            )
        with pytest.raises(QueryError, match="branch of union"):
            await (
                AggregationProduct.objects.filter(qty=1)
                .select_for_update()
                .union(AggregationProduct.objects.filter(qty=2))
            )


@pytest.mark.asyncio
async def test_unordered_slice_aggregate_takes_the_first_rows_by_primary_key(db):
    """An unordered slice's aggregate picked its rows by an unordered LIMIT subquery - the database
    could read them through an index and aggregate other rows than first()/last() of the slice."""
    first_author = await Author.objects.create(name="a")
    second_author = await Author.objects.create(name="b")
    # Ordered by the author index, the books come out as 2, 3, 1 - by primary key as 1, 2, 3.
    first_book = await Book.objects.create(name="b1", author=second_author, rating=1)
    second_book = await Book.objects.create(name="b2", author=first_author, rating=10)
    third_book = await Book.objects.create(name="b3", author=first_author, rating=100)

    assert await Book.objects.all()[:2].aggregate(total=Sum("rating"), n=Count("id")) == {"total": 11, "n": 2}
    assert await Book.objects.all()[1:3].aggregate(total=Sum("rating")) == {"total": 110}
    assert "ORDER BY" in Book.objects.all()[:2].aggregate(total=Sum("rating")).sql()
    assert (await Book.objects.all()[:2].first()).pk == first_book.pk
    assert (await Book.objects.all()[:2].last()).pk == second_book.pk
    assert (await Book.objects.all()[1:3].last()).pk == third_book.pk


@pytest.mark.asyncio
async def test_distinct_slice_aggregate_over_a_to_many_filter(db):
    """A sliced .distinct() queryset filtering over a to-many relation raised the fan-out
    QueryError - its rows are reduced to one per primary key first, as without a slice."""
    await create_aggregation_shops()
    first_tag = await AggregationTag.objects.create(name="t1")
    second_tag = await AggregationTag.objects.create(name="t2")
    products = {product.name: product for product in await AggregationProduct.objects.all()}
    await products["p1"].tags.add(first_tag, second_tag)
    await products["p2"].tags.add(first_tag)
    await products["p3"].tags.add(second_tag)
    tagged = AggregationProduct.objects.filter(tags__in=[first_tag, second_tag])

    assert await tagged.distinct().order_by("id")[:2].aggregate(total=Sum("qty"), n=Count("id")) == {
        "total": 5,
        "n": 2,
    }
    assert await tagged.distinct().order_by("-id")[1:].aggregate(total=Sum("qty")) == {"total": 5}
    assert await tagged.distinct().aggregate(total=Sum("qty")) == {"total": 6}
    with pytest.raises(QueryError, match="repeat a primary key"):
        await tagged.order_by("id")[:2].aggregate(total=Sum("qty"))


@pytest.mark.asyncio
async def test_null_keeping_conditions_dont_make_a_group_key_unique(db):
    """__in=[..., None] (matching NULL too) and a doubly negated condition (an EXISTS subquery) were
    taken as keeping NULL off the group key's JOIN."""
    assert (
        QueryJoins.get_non_null_filtered_lookups(
            Tournament.objects.filter(events__name__in=["x", None])._get_compiler()
        )
        == set()
    )
    assert (
        QueryJoins.get_non_null_filtered_lookups(
            Tournament.objects.filter(events__name__in=("x", None))._get_compiler()
        )
        == set()
    )
    assert QueryJoins.get_non_null_filtered_lookups(
        Tournament.objects.filter(events__name__in=["x"])._get_compiler()
    ) == {"events__name"}
    assert (
        QueryJoins.get_non_null_filtered_lookups(
            Tournament.objects.filter(~~Q(events__name__isnull=False))._get_compiler()
        )
        == set()
    )
    assert (
        QueryJoins.get_non_null_filtered_lookups(
            Tournament.objects.filter(~Q(events__name__isnull=True))._get_compiler()
        )
        == set()
    )
    assert QueryJoins.get_non_null_filtered_lookups(
        Tournament.objects.filter(Q(~~Q(events__name="x")) & Q(events__event_id__gt=0))._get_compiler()
    ) == {"events__event_id"}


@pytest.mark.asyncio
async def test_separate_filter_calls_get_their_own_join_past_a_forward_relation(db):
    """A to-many hop after a forward FK (product__tags) shared one JOIN across .filter() calls - two
    calls naming different tags matched nothing."""
    await create_aggregation_shops()
    first_tag = await AggregationTag.objects.create(name="t1")
    second_tag = await AggregationTag.objects.create(name="t2")
    products = {product.name: product for product in await AggregationProduct.objects.all()}
    await products["p1"].tags.add(first_tag, second_tag)
    await products["p2"].tags.add(first_tag)
    first_product_reviews = sorted(
        await AggregationReview.objects.filter(product=products["p1"]).values_list("id", flat=True)
    )

    def get_review_ids(queryset):
        return queryset.order_by("id").values_list("id", flat=True)

    assert (
        await get_review_ids(
            AggregationReview.objects.filter(product__tags=first_tag).filter(product__tags=second_tag)
        )
        == first_product_reviews
    )
    assert (
        await get_review_ids(
            AggregationReview.objects.filter(product__tags__id=first_tag.id).filter(product__tags__id=second_tag.id)
        )
        == first_product_reviews
    )
    assert (
        await get_review_ids(
            AggregationReview.objects.filter(product__tags__name="t1").filter(product__tags__name="t2")
        )
        == first_product_reviews
    )
    assert (
        await get_review_ids(
            AggregationReview.objects.filter(product__tags__name="t1", product__tags__name__in=["t2"])
        )
        == []
    )
    shop_a_reviews = await get_review_ids(AggregationReview.objects.filter(product__shop__name="A"))
    assert (
        await get_review_ids(
            AggregationReview.objects.filter(product__shop__orders__total=100).filter(product__shop__orders__total=50)
        )
        == shop_a_reviews
    )
    with pytest.raises(QueryError, match="product__tags"):
        await (
            AggregationReview.objects.filter(product__tags=first_tag)
            .filter(product__tags=second_tag)
            .annotate(n=Count("product__tags"))
            .values_list("id", "n")
        )
    # Each separate JOIN is pinned to one tag row by its primary key, so no review row repeats.
    first_product_stars = sum(
        await AggregationReview.objects.filter(product=products["p1"]).values_list("stars", flat=True)
    )
    assert await AggregationReview.objects.filter(product__tags=first_tag).filter(product__tags=second_tag).aggregate(
        total=Sum("stars")
    ) == {"total": first_product_stars}


@pytest.mark.asyncio
async def test_to_many_relation_equal_to_none_is_isnull(db):
    """Team.objects.filter(events=None) on a many-to-many relation raised ValidationError; like Django it is
    events__isnull=True, as it already was for a reverse FK."""
    tournament, both, _only_first, first_team, second_team, third_team = await _create_events_with_participants()
    empty_tournament = await Tournament.objects.create(name="empty")

    assert await Team.objects.filter(events=None).values_list("name", flat=True) == ["z"]
    assert await Team.objects.filter(Q(events=None) | Q(name="x")).values_list("name", flat=True) == ["x", "z"]
    assert await Team.objects.exclude(events=None).values_list("name", flat=True) == ["x", "y"]
    assert sorted(await Team.objects.filter(events__not=None).distinct().values_list("name", flat=True)) == ["x", "y"]
    assert await Tournament.objects.filter(events=None).values_list("name", flat=True) == ["empty"]
    assert empty_tournament.name == "empty" and tournament.name == "t"


@pytest.mark.asyncio
async def test_reverse_relation_to_a_composite_primary_key_takes_instances(db):
    """composite_owners=<instance> raised "needs a 2-tuple", composite_owners__in/__not/__not_in
    raised "Unknown filter param" - they compare every key column now."""
    first_tournament = await Tournament.objects.create(name="first")
    second_tournament = await Tournament.objects.create(name="second")
    await Tournament.objects.create(name="empty")
    first_owner = await CompositePkOwningFK.objects.create(a=1, b=1, name="one", tournament=first_tournament)
    await CompositePkOwningFK.objects.create(a=1, b=2, name="two", tournament=first_tournament)
    third_owner = await CompositePkOwningFK.objects.create(a=2, b=1, name="three", tournament=second_tournament)

    def get_names(queryset):
        return queryset.distinct().order_by("name").values_list("name", flat=True)

    assert await get_names(Tournament.objects.filter(composite_owners=first_owner)) == ["first"]
    assert await get_names(Tournament.objects.filter(composite_owners=(2, 1))) == ["second"]
    assert await get_names(Tournament.objects.filter(Q(composite_owners=third_owner))) == ["second"]
    assert await get_names(Tournament.objects.filter(composite_owners__in=[first_owner, (2, 1)])) == [
        "first",
        "second",
    ]
    assert await get_names(Tournament.objects.filter(composite_owners__in=[])) == []
    assert await get_names(Tournament.objects.filter(composite_owners__not=first_owner)) == [
        "empty",
        "first",
        "second",
    ]
    assert await get_names(Tournament.objects.filter(composite_owners__not_in=[first_owner, (1, 2)])) == [
        "empty",
        "second",
    ]
    assert await get_names(Tournament.objects.filter(composite_owners=None)) == ["empty"]
    assert await get_names(Tournament.objects.exclude(composite_owners=first_owner)) == ["empty", "second"]
    assert await get_names(
        Tournament.objects.filter(composite_owners=first_owner).filter(composite_owners__name="two")
    ) == ["first"]
    assert await get_names(Tournament.objects.filter(composite_owners=first_owner, composite_owners__name="two")) == []
    with pytest.raises(QueryError, match="needs a model instance or a 2-tuple"):
        await Tournament.objects.filter(composite_owners=1)
    with pytest.raises(QueryError, match="needs a model instance or a 2-tuple"):
        await Tournament.objects.filter(composite_owners__in=[(1, 2, 3)])
