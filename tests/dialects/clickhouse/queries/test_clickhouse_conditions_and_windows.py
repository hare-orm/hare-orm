"""CASE, COALESCE and NULLIF of Decimals of different scales, and the window functions ClickHouse names
otherwise or lacks - LAG/LEAD over the whole partition with NULL past its edge, CUME_DIST, and COUNT of
a Nullable column in a window."""

import decimal

import pytest

from hare.contrib.test import capture_queries
from hare.exceptions import UnSupportedError
from hare.query.expressions import Case, F, Value, When, Window
from hare.query.functions import Abs, Coalesce, NullIf, Sum
from hare.query.functions.window import Count, CumeDist, DenseRank, Lag, Lead, NthValue, NTile, Rank, RowNumber
from tests.dialects.clickhouse.column_types.test_clickhouse_containers import make_shipment
from tests.dialects.clickhouse.models import (
    Device,
    DeviceStatus,
    EscapedLabel,
    Labelled,
    Member,
    Player,
    Revision,
    Shipment,
    StatusArray,
    Team,
)


async def get_values(expression, **filters):
    return (
        await Player.objects.filter(**filters)
        .order_by("id")
        .annotate(value=expression)
        .values_list("value", flat=True)
    )


async def create_players():
    await Player.objects.bulk_create(
        [
            Player(id=1, name="first", score=decimal.Decimal("1.25"), rating=2.5, active=True),
            Player(id=2, name="second", score=decimal.Decimal("0"), rating=None, active=False),
            Player(id=3, name="third", score=decimal.Decimal("7"), rating=0.5, active=True),
        ]
    )


@pytest.mark.asyncio
async def test_decimals_of_different_scales_in_one_result(clickhouse_db):
    await create_players()

    coalesced = await get_values(Coalesce(NullIf("score", 0), Value(decimal.Decimal("0.125"))))
    assert [str(value) for value in coalesced] == ["1.250", "0.125", "7.000"]
    case = Case(When(active=True, then=F("score")), default=Value(decimal.Decimal("0.5")))
    assert [str(value) for value in await get_values(case)] == ["1.25", "0.50", "7.00"]
    case = Case(When(id=1, then=Value(decimal.Decimal("1.5"))), default=Value(decimal.Decimal("0.25")))
    assert [str(value) for value in await get_values(case)] == ["1.50", "0.25", "0.25"]


@pytest.mark.asyncio
async def test_null_if_compares_decimals_as_numbers(clickhouse_db):
    await create_players()

    assert await get_values(NullIf("score", 0)) == [decimal.Decimal("1.25"), None, decimal.Decimal("7")]
    assert await get_values(NullIf("score", Value(decimal.Decimal("7.000")))) == [
        decimal.Decimal("1.25"),
        decimal.Decimal("0"),
        None,
    ]


@pytest.mark.asyncio
async def test_lag_and_lead_read_the_whole_partition(clickhouse_db):
    await create_players()

    assert await get_values(Window(Lag("id"), order_by=["id"])) == [None, 1, 2]
    assert await get_values(Window(Lead("id"), order_by=["id"])) == [2, 3, None]
    assert await get_values(Window(Lead("id", 2, -1), order_by=["id"])) == [3, -1, -1]
    assert await get_values(Window(Lag("rating"), partition_by=["active"], order_by=["id"])) == [None, None, 2.5]
    assert await get_values(Window(Lead("score"), order_by=["id"])) == [
        decimal.Decimal("0"),
        decimal.Decimal("7"),
        None,
    ]


@pytest.mark.asyncio
async def test_cume_dist(clickhouse_db):
    await create_players()

    assert await get_values(Window(CumeDist(), order_by=["active"])) == [1.0, pytest.approx(1 / 3), 1.0]
    assert await get_values(Window(CumeDist(), order_by=["id"])) == [
        pytest.approx(1 / 3),
        pytest.approx(2 / 3),
        1.0,
    ]


@pytest.mark.asyncio
async def test_a_count_of_a_nullable_column_in_a_window(clickhouse_db):
    """ClickHouse up to 26.9 counts a Nullable column in a window through a subcolumn it then can't
    find (NOT_FOUND_COLUMN_IN_BLOCK) - the column is counted through identity()."""
    await create_players()

    assert await get_values(Window(Count("rating"), partition_by=["active"])) == [2, 0, 2]
    assert await get_values(Window(Count("rating"))) == [2, 2, 2]


@pytest.mark.asyncio
async def test_windows_over_a_composite_key_model(clickhouse_db):
    await Revision.objects.bulk_create(
        [
            Revision(document_id=1, number=1, title="a", score=1.5),
            Revision(document_id=1, number=2, title="b", score=None),
            Revision(document_id=2, number=1, title="c", score=4.0),
        ]
    )

    revisions = Revision.objects.order_by("document_id", "number").annotate(
        previous=Window(Lag("score"), partition_by=["document_id"], order_by=["number"]),
        counted=Window(Count("score"), partition_by=["document_id"]),
        share=Window(CumeDist(), partition_by=["document_id"], order_by=["number"]),
    )
    assert await revisions.values_list("previous", "counted", "share") == [
        (None, 1, 0.5),
        (1.5, 1, 1.0),
        (None, 1, 1.0),
    ]


@pytest.mark.asyncio
async def test_a_window_ordered_by_an_aggregate_of_a_relation_groups_the_rows(clickhouse_db):
    """ROW_NUMBER() OVER (ORDER BY SUM(...)) of a relation left the query ungrouped - code 215."""
    first = await Team.objects.create(name="first")
    second = await Team.objects.create(name="second")
    await Member.objects.bulk_create(
        [
            Member(id=1, club=first, number=1),
            Member(id=2, club=first, number=2),
            Member(id=3, club=second, number=9),
        ]
    )
    rows = await Team.objects.annotate(place=Window(RowNumber(), order_by=[Sum("members__number")])).values_list(
        "name", "place"
    )
    assert sorted(rows) == [("first", 1), ("second", 2)]


@pytest.mark.asyncio
async def test_a_row_number_a_rank_and_a_tile_mix_with_a_signed_integer(clickhouse_db):
    """ROW_NUMBER, RANK, DENSE_RANK and NTILE give a UInt64, which ClickHouse finds no common type of
    with a signed integer in CASE or COALESCE."""
    await create_players()

    for function in (RowNumber(), Rank(), DenseRank(), NTile(3)):
        window = Window(function, order_by=["id"])
        assert await get_values(Case(When(id__lte=2, then=window), default=Value(0))) == [1, 2, 0]
        assert await get_values(Coalesce(window, Value(-1))) == [1, 2, 3]


@pytest.mark.asyncio
async def test_rows_are_ordered_by_a_collation(clickhouse_db):
    """Collate() was written COLLATE "en" in parentheses - ClickHouse takes a collation's name as a string
    and only after an ORDER BY key's direction."""
    from hare.query.functions import Collate

    await Player.objects.bulk_create(
        [Player(id=1, name="b", score=decimal.Decimal(0)), Player(id=2, name="A", score=decimal.Decimal(0))]
    )
    names = await Player.objects.order_by(Collate("name", "en").asc()).values_list("name", flat=True)
    assert names == ["A", "b"]
    names = await Player.objects.order_by(Collate("name", "en").desc()).values_list("name", flat=True)
    assert names == ["b", "A"]


@pytest.mark.asyncio
async def test_distinct_rows_are_ordered_by_a_collation(clickhouse_db):
    """distinct() ordered by Collate() selected the collation's ordering in a subquery - ClickHouse takes no
    COLLATE there, and the statement was a syntax error."""
    from hare.query.functions import Collate

    await Player.objects.bulk_create(
        [
            Player(id=1, name="b", score=decimal.Decimal(0)),
            Player(id=2, name="A", score=decimal.Decimal(0)),
            Player(id=3, name="b", score=decimal.Decimal(1)),
        ]
    )
    query = Player.objects.order_by(Collate("name", "en").asc()).distinct().values_list("name", flat=True)
    assert await query == ["A", "b"]


@pytest.mark.asyncio
async def test_distinct_models_are_ordered_by_a_collation(clickhouse_db):
    """distinct() of model rows selected an ordering by Collate() as an expression - ClickHouse takes no
    COLLATE in a selected column."""
    from hare.query.functions import Collate

    await Player.objects.bulk_create(
        [Player(id=1, name="b", score=decimal.Decimal(0)), Player(id=2, name="A", score=decimal.Decimal(0))]
    )
    players = await Player.objects.order_by(Collate("name", "en").asc(), "id").distinct()
    assert [player.name for player in players] == ["A", "b"]


@pytest.mark.asyncio
async def test_lag_and_lead_of_a_container_take_a_default_of_its_type(clickhouse_db):
    """The value was made nullable - lagInFrame(toNullable("tags"), ...) failed: "Nested type Array(String)
    cannot be inside Nullable type"."""
    for shipment_id, tag, currency in ((1, "a", "eur"), (2, "b", "usd"), (3, "c", "gbp")):
        await make_shipment(
            shipment_id,
            tags=[tag],
            prices={currency: decimal.Decimal(shipment_id)},
            point={"lon": float(shipment_id), "lat": 0.0},
        ).save()
    shipments = Shipment.objects.order_by("id")
    rows = await shipments.annotate(
        previous_tags=Window(Lag("tags", 1, []), order_by=["id"]),
        next_prices=Window(Lead("prices", 1, {}), order_by=["id"]),
        previous_point=Window(Lag("point", 2, {"lon": 0.0, "lat": 0.0}), order_by=["id"]),
        same_tags=Window(Lag("tags", 0), order_by=["id"]),
    ).values_list("previous_tags", "next_prices", "previous_point", "same_tags")
    assert rows == [
        ([], {"usd": decimal.Decimal("2.00")}, {"lon": 0.0, "lat": 0.0}, ["a"]),
        (["a"], {"gbp": decimal.Decimal("3.00")}, {"lon": 0.0, "lat": 0.0}, ["b"]),
        (["b"], {}, {"lon": 1.0, "lat": 0.0}, ["c"]),
    ]
    await StatusArray.objects.create(id=1, statuses=[DeviceStatus.ONLINE])
    await StatusArray.objects.create(id=2, statuses=[])
    copies = StatusArray.objects.order_by("id").annotate(previous=Window(Lag("statuses_copy", 1, []), order_by=["id"]))
    assert await copies.values_list("previous", flat=True) == [[], [DeviceStatus.ONLINE]]


@pytest.mark.asyncio
async def test_a_null_past_the_partition_of_a_container_is_refused_before_any_sql(clickhouse_db):
    """A container holds no NULL - the value past the partition's edge has nothing to be."""
    for expression in (Lag("tags"), Lead("prices", 2), NthValue("tags", 2)):
        async with capture_queries() as counter:
            with pytest.raises(UnSupportedError, match="past the partition"):
                await Shipment.objects.annotate(value=Window(expression, order_by=["id"])).values_list("value")
        assert counter.count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["label", "label_copy"])
async def test_lag_and_lead_of_an_enum_take_a_default_label(clickhouse_db, field_name):
    """The default was written as the enum's number the column stores, beside the label the window
    reads - Lag("label", 1, State.RED) gave "-8305"."""
    await Labelled.objects.bulk_create(
        [Labelled(id=1, label=EscapedLabel.PLAIN), Labelled(id=2, label=EscapedLabel.NEW_LINE)]
    )
    rows = Labelled.objects.order_by("id")
    for default in (EscapedLabel.QUOTE, EscapedLabel.BACKSLASH, "it's"):
        assert await rows.annotate(previous=Window(Lag(field_name, 1, default), order_by=["id"])).values_list(
            "previous", flat=True
        ) == [EscapedLabel(default), EscapedLabel.PLAIN]
    following = rows.annotate(following=Window(Lead(field_name, 1, EscapedLabel.TAB), order_by=["id"]))
    values = await following.values_list("following", flat=True)
    assert values == [EscapedLabel.NEW_LINE, EscapedLabel.TAB]
    assert all(type(value) is EscapedLabel for value in values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "first", "default"),
    [
        ("huge", 1, -(2**200)),
        ("huge", 1, 2**255 - 1),
        ("huge", 1, -(2**255)),
        ("huge", 1, 2**127),
        ("big", 1, 2**64 - 1),
        ("big", 1, 2**63),
    ],
)
async def test_lag_of_a_wide_integer_takes_its_default_unchanged(clickhouse_db, field_name, first, default):
    """A default past 64 bits was cast to a NUMERIC - toDecimal256(value, 38) - keeping 38 digits of it."""
    for device_id, value in ((1, first), (2, default)):
        await Device.objects.create(
            id=device_id,
            tiny=1,
            big=value if field_name == "big" else 1,
            huge=value if field_name == "huge" else None,
            ratio=1.0,
            code="abcd",
            country="DE",
            status="online",
            address="10.0.0.1",
            labels=[],
        )
    rows = Device.objects.order_by("id")
    for given in (default, Value(default)):
        assert await rows.annotate(previous=Window(Lag(field_name, 1, given), order_by=["id"])).values_list(
            "previous", flat=True
        ) == [default, first]


@pytest.mark.asyncio
@pytest.mark.parametrize("wide", [-(2**200), 2**200, 2**127, -(2**255)])
async def test_a_wide_integer_literal_keeps_its_width_in_a_function(clickhouse_db, wide):
    """A bare literal was cast to toDecimal256(..., 38) - no common type with an Int256 in Coalesce() - and
    Abs() of one converted back to the literal's shared Int64 field: 0."""
    for device_id, huge in ((1, None), (2, wide), (3, 1)):
        await Device.objects.create(
            id=device_id,
            tiny=1,
            big=1,
            huge=huge,
            ratio=1.0,
            code="abcd",
            country="DE",
            status="online",
            address="10.0.0.1",
            labels=[],
        )
    rows = Device.objects.order_by("id")
    for given in (wide, Value(wide)):
        assert await rows.annotate(value=Coalesce("huge", given)).values_list("value", flat=True) == [wide, wide, 1]
    if wide != -(2**255):
        # The absolute value of Int256's minimum is past Int256 - as abs() of the column gives it.
        for given in (wide, Value(wide)):
            assert await rows.annotate(value=Abs(given)).values_list("value", flat=True) == [abs(wide)] * 3
