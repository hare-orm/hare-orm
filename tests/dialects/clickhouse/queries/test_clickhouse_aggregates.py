"""ClickHouse's own aggregates - distinct counts, quantiles, the value at an extremum, values
collected into arrays, the most frequent ones, maps added up - with ``distinct=`` and ``_filter=``,
and ``ArrayAgg`` collected by ``groupArray``."""

import decimal

import pytest
import pytest_asyncio

from hare.contrib.test import capture_queries
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
from hare.dialects.postgresql.functions import JSONBAgg
from hare.exceptions import QueryError, UnSupportedError
from hare.query.expressions import F, Q, Value, Window
from hare.query.functions import Abs, ArrayAgg, Avg, Coalesce, Count, Sum
from hare.query.functions.window import Avg as WindowAvg, Sum as WindowSum
from tests.dialects.clickhouse.column_types.test_clickhouse_containers import make_shipment
from tests.dialects.clickhouse.models import Account, Country, Device, Player, Quote, Shipment


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
    by_expression = await (
        Player.objects.filter(active=True)
        .values("active")
        .annotate(
            newest=ArrayAgg("name", order_by=F("id") * -1),
            best=ArrayAgg("name", order_by=Coalesce("rating", Value(0.0)).desc()),
        )
    )
    assert by_expression[0]["newest"] == ["c", "a", "b"]
    assert by_expression[0]["best"] == ["b", "c", "a"]
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


@pytest.mark.asyncio
async def test_a_sum_of_64_bit_integers_passing_their_range_wraps_around_as_clickhouse_sums(clickhouse_db):
    """sum() of an Int64 is ClickHouse's own Int64 - hare widens nothing."""
    await Account.objects.bulk_create([Account(id=2**62 + number, owner="a", balance=2**30) for number in range(4)])
    totals = await Account.objects.aggregate(total=Sum("id"), balances=Sum("balance"))
    assert totals == {"total": 6, "balances": 2**32}
    assert "toInt128" not in Account.objects.annotate(total=Sum("id")).sql()


@pytest.mark.asyncio
async def test_clickhouse_aggregates_over_no_rows_give_none_for_a_column_of_no_null(quotes):
    """Over no rows of a column holding no NULL, ClickHouse gives the type's default or NaN - quantile()
    NaN, argMin()/any() of a text '' - read as values no row holds; their -OrNull forms give NULL."""
    result = await Quote.objects.filter(symbol="nothing").aggregate(
        quantile=Quantile("price", 0.5),
        exact=Quantile("quoted_at", 0.9, exact=True),
        median=Median("quoted_at"),
        first=ArgMin("symbol", "quoted_at"),
        last=ArgMax("symbol", "quoted_at"),
        any_value=AnyValue("symbol"),
        any_last=AnyLast("symbol"),
    )
    assert result == dict.fromkeys(result, None)


@pytest.mark.asyncio
async def test_an_aggregate_named_as_a_column_leaves_the_filters_reading_the_column(clickhouse_db):
    """`SUM(balance) AS balance` - ClickHouse read the filter's `balance` as the alias: an aggregate in WHERE."""
    await Account.objects.bulk_create(
        [
            Account(id=1, owner="a", balance=1),
            Account(id=2, owner="b", balance=2),
            Account(id=3, owner="c", balance=10),
        ]
    )
    assert await Account.objects.filter(balance__gt=1).aggregate(balance=Sum("balance")) == {"balance": 12}
    assert await Account.objects.filter(owner="b").aggregate(owner=Count("id")) == {"owner": 1}


@pytest.mark.asyncio
async def test_an_absolute_value_of_a_signed_expression_combines_with_a_signed_branch(clickhouse_db):
    """`abs()` of an Int64 is a UInt64 on ClickHouse - Coalesce() with a signed nullable column found no
    common type (code 386)."""
    await Country.objects.bulk_create([Country(code="aa", name="a", population=-3), Country(code="bb", name="b")])
    rows = await (
        Country.objects.annotate(magnitude=Coalesce(Abs(F("population") + F("population")), F("population")))
        .order_by("code")
        .values_list("magnitude", flat=True)
    )
    assert rows == [6, None]
    # The minimum's absolute value isn't an Int64 - ClickHouse's conversion keeps the minimum.
    await Country.objects.create(code="cc", name="c", population=-(2**63))
    assert await Country.objects.filter(code="cc").annotate(magnitude=Abs(F("population"))).values_list(
        "magnitude", flat=True
    ) == [-(2**63)]


@pytest.mark.asyncio
async def test_an_absolute_value_of_an_aggregate_is_computed(clickhouse_db):
    """abs() was cast to toTypeName() of its argument - of an aggregate no constant, refused (code 43)."""
    await Country.objects.bulk_create(
        [Country(code="aa", name="a", population=-3), Country(code="bb", name="b", population=-4)]
    )
    assert await Country.objects.aggregate(magnitude=Abs(Sum("population"))) == {"magnitude": 7}


@pytest.mark.asyncio
async def test_an_aggregate_of_nothing_is_an_empty_dict(clickhouse_db):
    """It sent a select list of nothing, which ClickHouse refuses - every other database read it as {}."""
    assert await Player.objects.filter(active=True).aggregate() == {}


@pytest.mark.asyncio
async def test_a_sum_an_average_and_arithmetic_of_float32_values_keep_their_float64_result(clickhouse_db):
    """ClickHouse computes them as a Float64 - they were read through the float32 field, which rounded the
    result to a float32: 16777217 came back as 16777216, 8388608.5 as 8388608."""
    for device_id, ratio in ((1, 16777216.0), (2, 1.0)):
        await Device.objects.create(
            id=device_id,
            tiny=1,
            big=1,
            ratio=ratio,
            code="abcd",
            country="DE",
            status="online",
            address="10.0.0.1",
            labels=[],
        )
    totals = await Device.objects.aggregate(total=Sum("ratio"), mean=Avg("ratio"))
    assert totals == {"total": 16777217.0, "mean": 8388608.5}
    windowed = await Device.objects.annotate(
        total=Window(WindowSum("ratio")), mean=Window(WindowAvg("ratio"))
    ).values_list("total", "mean")
    assert set(windowed) == {(16777217.0, 8388608.5)}
    assert await Device.objects.filter(id=2).annotate(shifted=F("ratio") + 1e-8).values_list("shifted", flat=True) == [
        1.00000001
    ]
    # A float32 read as it is stored keeps its shortest form.
    await Device.objects.filter(id=2).update(ratio=0.1)
    assert await Device.objects.filter(id=2).values_list("ratio", flat=True) == [0.1]


@pytest.mark.asyncio
async def test_a_function_of_another_database_is_refused_before_any_sql(clickhouse_db):
    """JSONB_AGG went to ClickHouse - its own unknown-function error instead of hare's refusal."""
    async with capture_queries() as counter:
        with pytest.raises(UnSupportedError, match=r"JSONBAgg\(\) is a function of the postgresql database"):
            await Player.objects.values("team_id").annotate(names=JSONBAgg("name"))
    assert counter.count == 0
