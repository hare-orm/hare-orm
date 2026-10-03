from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from hare.exceptions import FieldError, QueryError
from hare.query.expressions import F, Q, Subquery, Window
from hare.query.expressions.base import ExpressionResult
from hare.query.functions import Count, Sum as PlainSum
from hare.query.functions.window import (
    Avg,
    Count as WindowCount,
    FirstValue,
    Lag,
    LastValue,
    Lead,
    Max,
    Min,
    Rank,
    RowNumber,
    Sum,
)
from hare.sql import Table
from tests.testmodels import (
    Author,
    Book,
    CharFields,
    CompositePkThing,
    DateFields,
    DatetimeFields,
    DecimalFields,
    Event,
    IntFields,
    JSONFields,
    SoftDeleteStandalone,
    Team,
    Tournament,
)


@pytest_asyncio.fixture
async def intfields_data(db):
    """Three partitions (intnum_null in {0, 1, 2}), two rows each, ordered by intnum."""
    return [
        await IntFields.objects.create(intnum=intnum, intnum_null=partition)
        for partition in range(3)
        for intnum in (partition * 10, partition * 10 + 1)
    ]


@pytest.mark.asyncio
async def test_row_number_partitioned(db, intfields_data):
    rows = (
        await IntFields.objects.all()
        .annotate(rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"]))
        .order_by("intnum_null", "intnum")
        .values("intnum_null", "intnum", "rn")
    )
    assert [row["rn"] for row in rows] == [1, 2, 1, 2, 1, 2]


@pytest.mark.asyncio
async def test_windowed_sum(db, intfields_data):
    rows = (
        await IntFields.objects.all()
        .annotate(running_total=Window(Sum("intnum"), partition_by=["intnum_null"], order_by=["intnum"]))
        .order_by("intnum_null", "intnum")
        .values("intnum_null", "intnum", "running_total")
    )
    assert [row["running_total"] for row in rows] == [0, 1, 10, 21, 20, 41]


@pytest.mark.asyncio
async def test_lag_lead(db, intfields_data):
    rows = (
        await IntFields.objects.all()
        .annotate(
            prev=Window(Lag("intnum"), partition_by=["intnum_null"], order_by=["intnum"]),
            next=Window(Lead("intnum"), partition_by=["intnum_null"], order_by=["intnum"]),
        )
        .order_by("intnum_null", "intnum")
        .values("intnum_null", "intnum", "prev", "next")
    )
    assert [row["prev"] for row in rows] == [None, 0, None, 10, None, 20]
    assert [row["next"] for row in rows] == [1, None, 11, None, 21, None]


@pytest.mark.asyncio
async def test_first_value_partitioned(db, intfields_data):
    rows = (
        await IntFields.objects.all()
        .annotate(first=Window(FirstValue("intnum"), partition_by=["intnum_null"], order_by=["intnum"]))
        .order_by("intnum_null", "intnum")
        .values("intnum_null", "intnum", "first")
    )
    assert [row["first"] for row in rows] == [0, 0, 10, 10, 20, 20]


@pytest.mark.asyncio
async def test_last_value_partitioned(db, intfields_data):
    """LastValue(field) must return the field's value on the partition's actual LAST row for
    EVERY row, not the current row's own value. Window.get_result() used to never set an
    explicit SQL frame, so with order_by set, SQL's own default frame (RANGE BETWEEN UNBOUNDED
    PRECEDING AND CURRENT ROW) made LAST_VALUE always equal the current row - confirmed live
    before the fix: `last` came back as [0, 1, 10, 11, 20, 21] (each row's own intnum) instead of
    the partition's last value repeated for every row."""
    rows = (
        await IntFields.objects.all()
        .annotate(last=Window(LastValue("intnum"), partition_by=["intnum_null"], order_by=["intnum"]))
        .order_by("intnum_null", "intnum")
        .values("intnum_null", "intnum", "last")
    )
    assert [row["last"] for row in rows] == [1, 1, 11, 11, 21, 21]


@pytest.mark.asyncio
async def test_last_value_three_row_partition(db):
    """The exact scenario from the original bug report: a single partition of three rows -
    LastValue must equal the partition's last value (15) for every row, not each row's own
    intnum."""
    for intnum in (5, 10, 15):
        await IntFields.objects.create(intnum=intnum, intnum_null=0)

    rows = (
        await IntFields.objects.all()
        .annotate(last=Window(LastValue("intnum"), partition_by=["intnum_null"], order_by=["intnum"]))
        .order_by("intnum")
        .values("intnum", "last")
    )
    assert [row["last"] for row in rows] == [15, 15, 15]


@pytest.mark.asyncio
async def test_window_does_not_break_plain_aggregates(db, intfields_data):
    total = await IntFields.objects.all().aggregate(total=PlainSum("intnum"))
    assert total["total"] == sum(row["intnum"] for row in await IntFields.objects.all().values("intnum"))


# ============================================================================
# Combinations with the rest of the QuerySet API
# ============================================================================


@pytest.mark.asyncio
async def test_window_alias_cannot_be_used_in_filter(db, intfields_data):
    """SQL itself doesn't allow a window function in WHERE - filtering on one raises a clear,
    ORM-level error at query-build time instead of a raw backend SQL error."""
    with pytest.raises(QueryError, match="window function"):
        await (
            IntFields.objects.all()
            .annotate(rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"]))
            .filter(rn=1)
        )


@pytest.mark.asyncio
async def test_window_nested_in_another_windows_partition_by_raises(db, intfields_data):
    """A partition_by/order_by name that resolves to ANOTHER Window(...) annotation used to
    splice that whole other window function's own OVER (...) term straight into this window's
    partition clause (F.get_result()'s generic "reference to another annotation" branch has no
    special case for a Window) - nesting one window function inside another's own window
    definition, which every SQL dialect rejects outright. Must raise a clear ORM-level error
    instead of letting the malformed SQL reach the database."""
    with pytest.raises(QueryError, match="Window"):
        await (
            IntFields.objects.all()
            .annotate(
                rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"]),
                rn2=Window(RowNumber(), partition_by=["rn"]),
            )
            .values("rn2")
        )


@pytest.mark.asyncio
async def test_window_nested_in_another_windows_order_by_raises(db, intfields_data):
    """Same as above, for order_by instead of partition_by."""
    with pytest.raises(QueryError, match="Window"):
        await (
            IntFields.objects.all()
            .annotate(
                rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"]),
                rn2=Window(RowNumber(), order_by=["rn"]),
            )
            .values("rn2")
        )


@pytest.mark.asyncio
async def test_window_filter_wrapped_in_subquery_works_as_documented(db, intfields_data):
    """The documented workaround for "can't filter on a Window(...) annotation directly" (see
    test_window_alias_cannot_be_used_in_filter above and docs/querying/aggregation.md's own
    warning) - wrap the annotated queryset in a Subquery and filter on that
    instead. Used to raise the SAME ConfigurationError even wrapped in Subquery(), since nothing
    actually built the two-layer SQL (an inner derived table computing the window function,
    filtered by an outer query referencing its own column) the workaround describes - Subquery()
    just embeds the inner query's SQL as-is, and the inner query's own .filter(rn=1) raised
    before Subquery ever got a chance to wrap anything."""
    ranked_qs = IntFields.objects.all().annotate(
        rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"])
    )
    # "id", not "pk" - a literal "pk" never resolves in .values()/.values_list() for ANY model
    # (see test_composite_pk_literal_pk_not_resolvable_in_values), a separate, deliberate,
    # pre-existing design choice unrelated to this fix - "pk" only resolves as an alias in
    # .filter()/.get()/instance.pk.
    first_per_partition = await IntFields.objects.filter(pk__in=Subquery(ranked_qs.filter(rn=1).values("id")))
    assert sorted(obj.intnum for obj in first_per_partition) == [0, 10, 20]


@pytest.mark.asyncio
async def test_window_filter_wrapped_in_subquery_respects_a_non_window_filter_too(db, intfields_data):
    """The remaining (non-window) filter(s) on the same queryset must still apply - only the
    window-referencing kwarg needs the two-layer wrapping, everything else stays in the SAME
    inner query it always would."""
    ranked_qs = IntFields.objects.all().annotate(
        rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"])
    )
    first_in_partition_zero = await IntFields.objects.filter(
        pk__in=Subquery(ranked_qs.filter(rn=1, intnum_null=0).values("id"))
    )
    assert [obj.intnum for obj in first_in_partition_zero] == [0]


@pytest.mark.asyncio
async def test_values_list_filter_on_window_annotation_wraps_in_subquery_too(db, intfields_data):
    """`.values()` auto-wraps a `.filter(window_annotation=value)` into the same two-layer
    derived-table pattern `test_window_filter_wrapped_in_subquery_works_as_documented` above
    builds by hand with an explicit `Subquery()` - `ValuesListQuery` had no equivalent machinery
    at all (`_window_annotation_names()`/`_split_window_annotation_filters()`/`_make_query_
    wrapped_for_window_filter()` existed only on `ValuesQuery`), so the identical
    `.values_list(...).filter(window_annotation=value)` raised ConfigurationError instead of
    working transparently like `.values()` does."""
    ranked_qs = IntFields.objects.all().annotate(
        rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"])
    )

    rows = await ranked_qs.filter(rn=1).values_list("intnum", "rn")
    assert sorted(rows) == [(0, 1), (10, 1), (20, 1)]

    # The window annotation itself doesn't have to be part of the requested output either.
    flat_intnums = await ranked_qs.filter(rn=1).values_list("intnum", flat=True)
    assert sorted(flat_intnums) == [0, 10, 20]


@pytest.mark.asyncio
async def test_order_by_window_alias(db):
    for intnum in (30, 10, 20):
        await IntFields.objects.create(intnum=intnum, intnum_null=0)

    rows = (
        await IntFields.objects.all()
        .annotate(rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"]))
        .order_by("-rn")
        .values("intnum", "rn")
    )
    assert [row["rn"] for row in rows] == [3, 2, 1]


@pytest.mark.asyncio
async def test_window_with_values_list(db, intfields_data):
    rows = (
        await IntFields.objects.all()
        .annotate(rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"]))
        .order_by("intnum_null", "intnum")
        .values_list("intnum", "rn")
    )
    assert [rn for _, rn in rows] == [1, 2, 1, 2, 1, 2]


@pytest.mark.asyncio
async def test_window_with_only(db, intfields_data):
    rows = (
        await IntFields.objects.all()
        .annotate(rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"]))
        .only("intnum")
        .order_by("intnum_null", "intnum")
    )
    assert [(row.intnum, row.rn) for row in rows] == [(0, 1), (1, 2), (10, 1), (11, 2), (20, 1), (21, 2)]


@pytest.mark.asyncio
async def test_window_with_defer(db, intfields_data):
    rows = (
        await IntFields.objects.all()
        .annotate(rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"]))
        .defer("intnum_null")
        .order_by("intnum_null", "intnum")
    )
    assert [(row.intnum, row.rn) for row in rows] == [(0, 1), (1, 2), (10, 1), (11, 2), (20, 1), (21, 2)]


@pytest.mark.asyncio
async def test_window_partition_by_joined_field(db):
    """partition_by/order_by referencing a related field (forcing a JOIN) - exercises F()'s
    table-qualification, not just direct fields of the base model."""
    tournament = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=tournament)
    await Event.objects.create(name="E2", tournament=tournament)

    rows = (
        await Event.objects.all()
        .annotate(rn=Window(RowNumber(), partition_by=["tournament__name"], order_by=["name"]))
        .order_by("name")
        .values("name", "rn")
    )
    assert [row["rn"] for row in rows] == [1, 2]


@pytest.mark.asyncio
async def test_window_with_distinct(db, intfields_data):
    rows = await (
        IntFields.objects.all()
        .annotate(rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"]))
        .distinct()
        .values("intnum", "rn")
    )
    assert len(rows) == 6


@pytest.mark.asyncio
async def test_multiple_window_annotations(db, intfields_data):
    rows = (
        await IntFields.objects.all()
        .annotate(
            rn=Window(RowNumber(), partition_by=["intnum_null"], order_by=["intnum"]),
            rk=Window(Rank(), partition_by=["intnum_null"], order_by=["intnum"]),
        )
        .order_by("intnum_null", "intnum")
        .values("intnum_null", "intnum", "rn", "rk")
    )
    assert [row["rn"] for row in rows] == [1, 2, 1, 2, 1, 2]
    assert [row["rk"] for row in rows] == [1, 2, 1, 2, 1, 2]


@pytest.mark.asyncio
async def test_window_with_unrelated_prefetch_related(db):
    tournament = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=tournament)
    await Event.objects.create(name="E2", tournament=tournament)

    rows = (
        await Event.objects.all()
        .annotate(rn=Window(RowNumber(), partition_by=["tournament_id"], order_by=["name"]))
        .prefetch_related("tournament")
        .order_by("name")
    )
    assert [(row.name, row.rn, row.tournament.name) for row in rows] == [("E1", 1, "T1"), ("E2", 2, "T1")]


@pytest.mark.asyncio
async def test_window_on_composite_pk_model(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")
    await CompositePkThing.objects.create(thing_id=2, revision=1, name="C")

    rows = (
        await CompositePkThing.objects.all()
        .annotate(rn=Window(RowNumber(), partition_by=["thing_id"], order_by=["revision"]))
        .order_by("thing_id", "revision")
        .values("thing_id", "revision", "rn")
    )
    assert [row["rn"] for row in rows] == [1, 2, 1]


@pytest.mark.asyncio
async def test_window_respects_soft_delete_auto_filter(db):
    """The default manager's WHERE deleted_at IS NULL still applies - a soft-deleted row doesn't
    show up in the window's partition at all, not just excluded from the final result set."""
    await SoftDeleteStandalone.objects.create(name="alive")
    deleted = await SoftDeleteStandalone.objects.create(name="deleted")
    await deleted.delete()

    rows = (
        await SoftDeleteStandalone.objects.all()
        .annotate(rn=Window(RowNumber(), order_by=["name"]))
        .order_by("name")
        .values("name", "rn")
    )
    assert rows == [{"name": "alive", "rn": 1}]


@pytest.mark.asyncio
async def test_window_alongside_real_aggregate_annotation(db):
    """A Window(...) annotation sitting alongside a real aggregate annotation (Count/Sum/etc.)
    in the same .annotate() call must not get swept into the auto-generated GROUP BY that the
    aggregate triggers - SQL disallows window/analytic functions inside GROUP BY entirely, on
    both SQLite ("misuse of window function") and Postgres (42P20), unlike a plain non-aggregate
    column, which the auto-GROUP-BY correctly keeps."""
    t1 = await Tournament.objects.create(name="T1")
    t2 = await Tournament.objects.create(name="T2")
    await Event.objects.create(name="E1", tournament=t1)
    await Event.objects.create(name="E2", tournament=t1)
    await Event.objects.create(name="E3", tournament=t2)

    rows = (
        await Tournament.objects.all()
        .annotate(
            rn=Window(RowNumber(), order_by=["id"]),
            event_count=Count("events"),
        )
        .order_by("id")
        .values("id", "rn", "event_count")
    )
    assert [row["event_count"] for row in rows] == [2, 1]
    assert [row["rn"] for row in rows] == [1, 2]


@pytest.mark.asyncio
async def test_window_with_after_cursor(db, intfields_data):
    rows = (
        await IntFields.objects.all()
        .annotate(rn=Window(RowNumber(), order_by=["intnum"]))
        .order_by("intnum")
        .after_cursor(1)
        .values("intnum", "rn")
    )
    assert [row["intnum"] for row in rows][:2] == [10, 11]


@pytest.mark.asyncio
async def test_windowed_sum_over_decimal_field_decodes_through_the_fields_own_type(db):
    """Window.get_result() never set output_field at all, so a Window(Sum(...)) annotation
    over a DecimalField returned the raw driver value instead of a decoded Decimal - checked
    on a FIRST (cache miss) and a structurally identical SECOND (cache hit) call, since the
    QUERY_SHAPE_CACHE has its own separate history of losing annotation output fields on a hit."""
    a = await DecimalFields.objects.create(decimal=Decimal("1.5000"), decimal_nodec=Decimal("1"))
    b = await DecimalFields.objects.create(decimal=Decimal("2.5000"), decimal_nodec=Decimal("2"))

    first = (
        await DecimalFields.objects.filter(id=a.id)
        .annotate(running=Window(Sum("decimal"), partition_by=["id"]))
        .values("running")
    )
    second = (
        await DecimalFields.objects.filter(id=b.id)
        .annotate(running=Window(Sum("decimal"), partition_by=["id"]))
        .values("running")
    )

    assert first[0]["running"] == Decimal("1.5000")
    assert isinstance(first[0]["running"], Decimal)
    assert second[0]["running"] == Decimal("2.5000")
    assert isinstance(second[0]["running"], Decimal)


@pytest.mark.asyncio
async def test_rank_like_window_functions_do_not_inherit_the_partition_fields_type(db):
    """The opposite failure mode: RowNumber()/Rank() etc. have no underlying field at all, so
    partitioning/ordering by a DecimalField must not miscast their plain int result into
    Decimal."""
    a = await DecimalFields.objects.create(decimal=Decimal("1.5000"), decimal_nodec=Decimal("1"))
    b = await DecimalFields.objects.create(decimal=Decimal("2.5000"), decimal_nodec=Decimal("2"))

    first = (
        await DecimalFields.objects.filter(id=a.id)
        .annotate(rn=Window(RowNumber(), partition_by=["id"], order_by=["decimal"]))
        .values("rn")
    )
    second = (
        await DecimalFields.objects.filter(id=b.id)
        .annotate(rn=Window(RowNumber(), partition_by=["id"], order_by=["decimal"]))
        .values("rn")
    )
    assert isinstance(first[0]["rn"], int)
    assert isinstance(second[0]["rn"], int)


@pytest.mark.parametrize(
    "ordering_factory,expected_row_numbers",
    [
        (lambda: F("intnum_null").asc(nulls_first=True), {2: 1, 5: 2, 1: 3, 3: 4, 4: 5}),
        (lambda: F("intnum_null").asc(nulls_last=True), {1: 1, 3: 2, 4: 3, 2: 4, 5: 5}),
        (lambda: F("intnum_null").desc(nulls_first=True), {2: 1, 5: 2, 4: 3, 3: 4, 1: 5}),
        (lambda: F("intnum_null").desc(nulls_last=True), {4: 1, 3: 2, 1: 3, 2: 4, 5: 5}),
    ],
)
@pytest.mark.asyncio
async def test_window_order_by_accepts_explicit_null_placement(db, ordering_factory, expected_row_numbers):
    """Window(order_by=[...]) takes an F().asc()/.desc() ordering, so ROW_NUMBER() sees NULLs at the
    same position on every dialect."""
    for number, score in [(1, 1), (2, None), (3, 2), (4, 3), (5, None)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)

    rows = (
        await IntFields.objects.all()
        .annotate(rn=Window(RowNumber(), order_by=[ordering_factory(), "intnum"]))
        .values("intnum", "rn")
    )

    assert {row["intnum"]: row["rn"] for row in rows} == expected_row_numbers


@pytest_asyncio.fixture
async def typed_rows(db):
    """Two rows per test model, for window results over non-integer fields."""
    for intnum in (1, 2):
        await IntFields.objects.create(intnum=intnum)
    for char in ("abc", "a"):
        await CharFields.objects.create(char=char)
    for day in (date(2024, 1, 1), date(2024, 1, 2)):
        await DateFields.objects.create(date=day)
        await DatetimeFields.objects.create(datetime=datetime(2024, 1, day.day, tzinfo=UTC))
    for data in ({"a": 1}, {"a": 2}):
        await JSONFields.objects.create(data=data)
    for decimal in (Decimal("1.5"), Decimal("2.5")):
        await DecimalFields.objects.create(decimal=decimal, decimal_nodec=Decimal("1"))


@pytest.mark.asyncio
async def test_windowed_avg_of_int_field_is_a_fraction(typed_rows):
    """AVG over an IntField used to decode through that IntField, truncating 1.5 to 1."""
    for _ in range(2):  # cache miss, then cache hit
        values = await IntFields.objects.all().annotate(c=Window(Avg("intnum"))).values_list("c", flat=True)
        assert values == [1.5, 1.5]
        assert all(isinstance(value, float) for value in values)


@pytest.mark.asyncio
async def test_windowed_avg_of_decimal_field_stays_decimal(typed_rows):
    values = await DecimalFields.objects.all().annotate(c=Window(Avg("decimal"))).values_list("c", flat=True)
    assert values == [Decimal("2"), Decimal("2")]
    assert all(isinstance(value, Decimal) for value in values)


@pytest.mark.parametrize(
    "model,field_name",
    [
        (IntFields, "intnum"),
        (CharFields, "char"),
        (DatetimeFields, "datetime"),
        (DateFields, "date"),
        (JSONFields, "data"),
    ],
)
@pytest.mark.asyncio
async def test_windowed_count_is_an_integer_whatever_the_field_type(typed_rows, model, field_name):
    """COUNT used to decode through the counted field: '2' for text, a 1970 datetime, a
    ParseError for a date."""
    values = await model.objects.all().annotate(c=Window(WindowCount(field_name))).values_list("c", flat=True)
    assert values == [2, 2]
    assert all(type(value) is int for value in values)


@pytest.mark.asyncio
async def test_windowed_count_partitioned_and_ordered(typed_rows):
    rows = (
        await CharFields.objects.all()
        .annotate(c=Window(WindowCount("char"), partition_by=["char"], order_by=["id"]))
        .order_by("id")
        .values("char", "c")
    )
    assert rows == [{"char": "abc", "c": 1}, {"char": "a", "c": 1}]


@pytest.mark.asyncio
async def test_windowed_min_max_first_last_keep_the_field_type(typed_rows):
    rows = (
        await DateFields.objects.all()
        .annotate(
            low=Window(Min("date")),
            high=Window(Max("date")),
            first=Window(FirstValue("date"), order_by=["id"]),
            last=Window(LastValue("date"), order_by=["id"]),
        )
        .order_by("id")
        .values("low", "high", "first", "last")
    )
    expected = {"low": date(2024, 1, 1), "high": date(2024, 1, 2), "first": date(2024, 1, 1), "last": date(2024, 1, 2)}
    assert rows == [expected, expected]


@pytest.mark.asyncio
async def test_windowed_count_referenced_by_f(typed_rows):
    rows = (
        await DateFields.objects.all()
        .annotate(c=Window(WindowCount("date")))
        .annotate(doubled=F("c") * 2)
        .values_list("c", "doubled")
    )
    assert rows == [(2, 4), (2, 4)]


@pytest.mark.asyncio
async def test_lag_json_default_is_encoded_through_the_field(typed_rows):
    """A dict default used to reach the driver unencoded - rejected by SQLite and asyncpg."""
    for default in ({"d": 0}, {"d": [1, 2]}):  # cache miss, then cache hit with a new default
        values = (
            await JSONFields.objects.all()
            .annotate(c=Window(Lag("data", 1, default), order_by=["id"]))
            .order_by("id")
            .values_list("c", flat=True)
        )
        assert values == [default, {"a": 1}]


@pytest.mark.asyncio
async def test_lead_json_default_with_partition(typed_rows):
    values = (
        await JSONFields.objects.all()
        .annotate(c=Window(Lead("data", 1, ["none"]), partition_by=["id"], order_by=["id"]))
        .order_by("id")
        .values_list("c", flat=True)
    )
    assert values == [["none"], ["none"]]


@pytest.mark.asyncio
async def test_lag_lead_temporal_and_decimal_defaults(typed_rows):
    moments = (
        await DatetimeFields.objects.all()
        .annotate(c=Window(Lead("datetime", 1, datetime(2000, 1, 1, tzinfo=UTC)), order_by=["id"]))
        .order_by("id")
        .values_list("c", flat=True)
    )
    assert moments == [datetime(2024, 1, 2, tzinfo=UTC), datetime(2000, 1, 1, tzinfo=UTC)]
    days = (
        await DateFields.objects.all()
        .annotate(c=Window(Lag("date", 1, date(2000, 1, 1)), order_by=["id"]))
        .order_by("id")
        .values_list("c", flat=True)
    )
    assert days == [date(2000, 1, 1), date(2024, 1, 1)]
    decimals = (
        await DecimalFields.objects.all()
        .annotate(c=Window(Lead("decimal", 1, Decimal("9.25")), order_by=["id"]))
        .order_by("id")
        .values_list("c", flat=True)
    )
    assert decimals == [Decimal("2.5"), Decimal("9.25")]


@pytest.mark.asyncio
async def test_lag_int_default_that_fits_the_field(typed_rows):
    values = (
        await IntFields.objects.all()
        .annotate(c=Window(Lag("intnum", 1, -1), order_by=["id"]))
        .order_by("id")
        .values_list("c", flat=True)
    )
    assert values == [-1, 1]


@pytest.mark.parametrize("default", [0.5, Decimal("0.5"), "x"])
@pytest.mark.asyncio
async def test_lag_default_that_doesnt_fit_an_int_field_is_rejected(typed_rows, default):
    """A fractional default used to be silently truncated (0.5 came back as 0)."""
    with pytest.raises(QueryError, match="doesn't fit"):
        await IntFields.objects.all().annotate(c=Window(Lag("intnum", 1, default), order_by=["id"])).values_list("c")


@pytest.mark.asyncio
async def test_lag_default_that_doesnt_fit_is_rejected_on_a_cache_hit_too(typed_rows):
    def get_query(default):
        return IntFields.objects.all().annotate(c=Window(Lag("intnum", 1, default), order_by=["id"])).order_by("id")

    assert await get_query(7).values_list("c", flat=True) == [7, 1]
    with pytest.raises(QueryError, match="doesn't fit"):
        await get_query(0.5).values_list("c", flat=True)
    assert await get_query(8).values_list("c", flat=True) == [8, 1]


@pytest.mark.asyncio
async def test_window_over_count_annotation_has_count_types(db):
    """A window Sum()/Avg() over a Count() annotation decodes as int/float on every backend -
    Postgres returns SUM(bigint)/AVG(bigint) as numeric."""
    first = await Author.objects.create(name="first")
    second = await Author.objects.create(name="second")
    for rating in (1, 2, 3):
        await Book.objects.create(name=f"first {rating}", author=first, rating=rating)
    await Book.objects.create(name="second", author=second, rating=4)

    per_author = Book.objects.annotate(book_count=Count("id")).group_by("author_id")
    totals = await per_author.annotate(total=Window(Sum("book_count"))).values_list("total", flat=True)
    means = await per_author.annotate(mean=Window(Avg("book_count"))).values_list("mean", flat=True)

    assert totals == [4, 4]
    assert all(type(total) is int for total in totals)
    assert means == [2.0, 2.0]
    assert all(type(mean) is float for mean in means)


@pytest.mark.asyncio
async def test_window_accepts_an_aggregate_of_hare_query_functions(db):
    """Window(Count(...)) with an aggregate of hare.query.functions raised AttributeError - like
    Django, it is computed over the window, its _filter= as FILTER (WHERE ...)."""
    for intnum, group in ((1, 0), (2, 0), (3, 1)):
        await IntFields.objects.create(intnum=intnum, intnum_null=group)

    rows = (
        await IntFields.objects.all()
        .annotate(
            total=Window(PlainSum("intnum"), partition_by=["intnum_null"]),
            running=Window(PlainSum(F("intnum") * 2), order_by=["intnum"]),
            large=Window(Count("id", _filter=Q(intnum__gte=2)), partition_by=["intnum_null"]),
        )
        .order_by("intnum")
        .values_list("intnum", "total", "running", "large")
    )
    assert rows == [(1, 3, 2, 1), (2, 3, 6, 1), (3, 3, 12, 1)]
    with pytest.raises(QueryError, match="distinct=True"):
        Window(Count("id", distinct=True))


@pytest.mark.asyncio
async def test_iterator_pages_a_window_annotation_by_offset(db):
    """iterator() paged a queryset with a window annotation by keyset - each page's condition
    changed the rows the window was computed over (ROW_NUMBER restarted on every page)."""
    for intnum in range(1, 6):
        await IntFields.objects.create(intnum=intnum)

    numbered = IntFields.objects.all().annotate(position=Window(RowNumber(), order_by=["intnum"])).order_by("intnum")
    assert [(row.intnum, row.position) async for row in numbered.iterator(chunk_size=2)] == [
        (intnum, intnum) for intnum in range(1, 6)
    ]
    assert [row async for row in numbered.values_list("intnum", "position").iterator(chunk_size=2)] == [
        (intnum, intnum) for intnum in range(1, 6)
    ]


@pytest.mark.asyncio
async def test_window_aggregate_with_an_empty_filter_keeps_every_row(db):
    """Window(Count("id", _filter=Q())) raised NotImplementedError - an empty Q() was taken as a
    condition, unlike a plain aggregate's."""
    for intnum in (1, 2, 3):
        await IntFields.objects.create(intnum=intnum)

    for empty_filter in (Q(), ~Q(), Q(Q())):
        rows = (
            await IntFields.objects.all()
            .annotate(counted=Window(Count("id", _filter=empty_filter)))
            .order_by("intnum")
            .values_list("intnum", "counted")
        )
        assert rows == [(1, 3), (2, 3), (3, 3)]


@pytest.mark.asyncio
async def test_aggregate_filter_reading_an_aggregate_raises_field_error(db):
    """A _filter= reading another aggregate annotation rendered an empty FILTER (WHERE) - a bare
    NotImplementedError; Django raises FieldError."""
    for intnum in (1, 1, 2):
        await IntFields.objects.create(intnum=intnum)
    grouped = IntFields.objects.all().values("intnum")

    with pytest.raises(FieldError, match="reads an aggregate annotation"):
        await grouped.annotate(n=Count("id"), large=Count("id", _filter=Q(n__gt=1))).values_list("intnum", "large")
    with pytest.raises(FieldError, match="reads an aggregate annotation"):
        await grouped.annotate(n=Count("id"), large=Window(Count("id", _filter=Q(n__gt=1)))).values_list(
            "intnum", "large"
        )
    # A filter on a group key is still fine.
    assert await grouped.annotate(n=Count("id"), large=Window(PlainSum("n", _filter=Q(intnum__gt=1)))).order_by(
        "intnum"
    ).values_list("intnum", "large") == [(1, 1), (2, 1)]


@pytest.mark.asyncio
async def test_window_over_a_to_many_expression_joins_in_order(db):
    """The JOINs of an expression over a to-many relation were merged through a set - the through
    table's JOIN could come after the related table's, depending on PYTHONHASHSEED."""
    tournament = await Tournament.objects.create(name="t")
    first_event = await Event.objects.create(name="a", tournament=tournament, alias=2)
    second_event = await Event.objects.create(name="b", tournament=tournament, alias=5)
    team = await Team.objects.create(name="x")
    await first_event.participants.add(team)
    await second_event.participants.add(team)

    rows = await Team.objects.annotate(doubled=Window(PlainSum(F("events__alias") * 2))).values_list("id", "doubled")
    assert rows == [(team.id, 14), (team.id, 14)]

    joins = [(Table(f"table_{index}"), f"criterion_{index}") for index in range(32)]
    assert ExpressionResult.dedup_joins(joins[:20], joins[10:]) == joins
