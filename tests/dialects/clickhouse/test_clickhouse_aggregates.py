"""ClickHouse's own aggregates - distinct counts, quantiles, the value at an extremum, values
collected into arrays, the most frequent ones, maps added up - with ``distinct=`` and ``_filter=``,
and ``ArrayAgg`` collected by ``groupArray``."""

import decimal

import pytest
import pytest_asyncio

from hare.dialects.clickhouse.functions import (
    AnyLast,
    AnyValue,
    ArgMax,
    ArgMin,
    GroupArray,
    GroupUniqArray,
    Median,
    Quantile,
    Quantiles,
    SumMap,
    TopK,
    Uniq,
    UniqCombined,
    UniqExact,
)
from hare.exceptions import QueryError, UnSupportedError
from hare.query.expressions import Q
from hare.query.functions import ArrayAgg
from tests.dialects.clickhouse.models import Player, Quote, Shipment
from tests.dialects.clickhouse.test_clickhouse_containers import make_shipment


@pytest_asyncio.fixture
async def quotes(clickhouse_db):
    await Quote.objects.bulk_create(
        [
            Quote(id=1, symbol="a", quoted_at=10, price=decimal.Decimal("5.00")),
            Quote(id=2, symbol="a", quoted_at=20, price=decimal.Decimal("7.00")),
            Quote(id=3, symbol="a", quoted_at=30, price=decimal.Decimal("6.00")),
            Quote(id=4, symbol="b", quoted_at=15, price=decimal.Decimal("1.00")),
            Quote(id=5, symbol="b", quoted_at=25, price=decimal.Decimal("1.00")),
        ]
    )


@pytest.mark.asyncio
async def test_distinct_counts(quotes):
    counts = await Quote.objects.aggregate(
        symbols=Uniq("symbol"),
        exact=UniqExact("price"),
        combined=UniqCombined("symbol"),
        late=UniqExact("symbol", _filter=Q(quoted_at__gt=25)),
    )
    assert counts == {"symbols": 2, "exact": 4, "combined": 2, "late": 1}
    assert await Quote.objects.values("symbol").annotate(prices=UniqExact("price")).order_by("symbol") == [
        {"symbol": "a", "prices": 3},
        {"symbol": "b", "prices": 1},
    ]


@pytest.mark.asyncio
async def test_quantiles(quotes):
    result = await Quote.objects.filter(symbol="a").aggregate(
        middle=Median("price", exact=True),
        estimate=Median("quoted_at"),
        high=Quantile("quoted_at", 1, exact=True),
        spread=Quantiles("quoted_at", 0, 0.5, 1, exact=True),
        estimates=Quantiles("quoted_at", 0.5),
        filtered=Quantile("quoted_at", 0.5, exact=True, _filter=Q(quoted_at__gt=10)),
    )
    assert result["middle"] == decimal.Decimal("6.00")
    # An estimate of integers is interpolated - a float; an exact quantile is one of the values.
    assert result["estimate"] == 20.0 and isinstance(result["estimate"], float)
    assert result["high"] == 30 and isinstance(result["high"], int)
    assert result["spread"] == [10, 20, 30]
    assert result["estimates"] == [20.0]
    assert result["filtered"] == 30
    for level in (-0.1, 1.5, "0.5", True):
        with pytest.raises(QueryError):
            Quantile("price", level)
    with pytest.raises(QueryError):
        Quantiles("price")


@pytest.mark.asyncio
async def test_the_value_at_an_extremum(quotes):
    rows = await (
        Quote.objects.values("symbol")
        .annotate(
            first_price=ArgMin("price", "quoted_at"),
            last_price=ArgMax("price", "quoted_at"),
            any_price=AnyValue("price"),
            last_seen=AnyLast("symbol"),
        )
        .order_by("symbol")
    )
    assert [(row["symbol"], row["first_price"], row["last_price"], row["last_seen"]) for row in rows] == [
        ("a", decimal.Decimal("5.00"), decimal.Decimal("6.00"), "a"),
        ("b", decimal.Decimal("1.00"), decimal.Decimal("1.00"), "b"),
    ]
    assert rows[0]["any_price"] in (decimal.Decimal("5.00"), decimal.Decimal("7.00"), decimal.Decimal("6.00"))


@pytest.mark.asyncio
async def test_values_collected_into_arrays(quotes):
    rows = await (
        Quote.objects.values("symbol")
        .annotate(
            prices=GroupArray("price"),
            first=GroupArray("quoted_at", max_size=2),
            distinct_prices=GroupUniqArray("price"),
            popular=TopK("price", 1),
        )
        .order_by("symbol")
    )
    assert sorted(rows[0]["prices"]) == [decimal.Decimal("5.00"), decimal.Decimal("6.00"), decimal.Decimal("7.00")]
    assert len(rows[0]["first"]) == 2
    assert rows[1]["distinct_prices"] == [decimal.Decimal("1.00")]
    assert rows[1]["popular"] == [decimal.Decimal("1.00")]
    for arguments in ({"max_size": 0}, {"max_size": "2"}):
        with pytest.raises(QueryError):
            GroupArray("price", **arguments)
    with pytest.raises(QueryError):
        TopK("price", 0)


@pytest.mark.asyncio
async def test_array_agg_keeps_nulls_and_orders(clickhouse_db):
    await Player.objects.bulk_create(
        [
            Player(id=1, name="b", rating=2.0, active=True),
            Player(id=2, name="a", rating=None, active=True),
            Player(id=3, name="c", rating=1.0, active=True),
            Player(id=4, name="a", rating=5.0, active=False),
        ]
    )
    rows = await (
        Player.objects.values("active")
        .annotate(
            names=ArrayAgg("name", order_by="name"),
            newest_first=ArrayAgg("name", order_by="-id"),
            ratings=ArrayAgg("rating", order_by="id"),
            distinct_names=ArrayAgg("name", distinct=True),
            rated=ArrayAgg("name", order_by="id", _filter=Q(rating__isnull=False)),
        )
        .order_by("active")
    )
    assert rows[1]["names"] == ["a", "b", "c"]
    assert rows[1]["newest_first"] == ["c", "a", "b"]
    # groupArray alone would leave the NULL out.
    assert rows[1]["ratings"] == [2.0, None, 1.0]
    assert sorted(rows[1]["distinct_names"]) == ["a", "b", "c"]
    assert rows[1]["rated"] == ["b", "c"]
    assert rows[0]["names"] == ["a"]
    with pytest.raises(UnSupportedError):
        await Player.objects.values("active").annotate(names=ArrayAgg("name", order_by=["name", "-id"]))


@pytest.mark.asyncio
async def test_maps_are_added_up(clickhouse_db):
    await Shipment.objects.bulk_create(
        [
            make_shipment(1, prices={"eur": decimal.Decimal("1.50"), "usd": decimal.Decimal("2.00")}),
            make_shipment(2, prices={"eur": decimal.Decimal("0.50")}),
        ]
    )
    assert await Shipment.objects.aggregate(totals=SumMap("prices")) == {
        "totals": {"eur": decimal.Decimal("2.00"), "usd": decimal.Decimal("2.00")}
    }


@pytest.mark.asyncio
async def test_aggregates_over_a_window(quotes):
    from hare.query.expressions import Window

    rows = await (
        Quote.objects.annotate(
            prices=Window(UniqExact("price"), partition_by=["symbol"]),
            typical=Window(Quantile("quoted_at", 0.5, exact=True), partition_by=["symbol"]),
            last_price=Window(ArgMax("price", "quoted_at"), partition_by=["symbol"]),
        )
        .order_by("id")
        .values_list("id", "prices", "typical", "last_price")
    )
    assert rows == [
        (1, 3, 20, decimal.Decimal("6.00")),
        (2, 3, 20, decimal.Decimal("6.00")),
        (3, 3, 20, decimal.Decimal("6.00")),
        (4, 1, 25, decimal.Decimal("1.00")),
        (5, 1, 25, decimal.Decimal("1.00")),
    ]
