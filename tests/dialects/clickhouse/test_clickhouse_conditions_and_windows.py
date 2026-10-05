"""CASE, COALESCE and NULLIF of Decimals of different scales, and the window functions ClickHouse names
otherwise or lacks - LAG/LEAD over the whole partition with NULL past its edge, CUME_DIST, and COUNT of
a Nullable column in a window."""

import decimal

import pytest

from hare.query.expressions import Case, F, Value, When, Window
from hare.query.functions import Coalesce, NullIf
from hare.query.functions.window import Count, CumeDist, FirstValue, Lag, LastValue, Lead
from tests.dialects.clickhouse.models import Player, Revision


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
async def test_first_and_last_value_keep_a_null_row(clickhouse_db):
    await create_players()

    assert await get_values(Window(FirstValue("rating"), order_by=["id"]), id__gte=2) == [None, None]
    assert await get_values(Window(LastValue("rating"), order_by=["id"]), id__lte=2) == [None, None]


@pytest.mark.asyncio
async def test_cume_dist_and_a_count_of_a_nullable_column(clickhouse_db):
    await create_players()

    assert await get_values(Window(CumeDist(), order_by=["active"])) == [1.0, pytest.approx(1 / 3), 1.0]
    assert await get_values(Window(CumeDist(), order_by=["id"])) == [
        pytest.approx(1 / 3),
        pytest.approx(2 / 3),
        1.0,
    ]
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
