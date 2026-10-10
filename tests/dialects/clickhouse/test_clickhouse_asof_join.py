"""AsofJoin on ClickHouse - each trade priced by the last quote of its symbol at or before it: the
SQL, the rows, the NULLs of a trade without one, filters through it and its plan."""

import decimal

import pytest
import pytest_asyncio

from hare.exceptions import QueryError
from hare.query.expressions import AsofJoin, OuterReference, Q
from hare.query.plans.statement.statement_plans import StatementPlans
from tests.dialects.clickhouse.models import Quote, Trade


@pytest_asyncio.fixture
async def market(clickhouse_db):
    await Quote.objects.bulk_create(
        [
            Quote(id=1, symbol="A", quoted_at=10, price=decimal.Decimal("1.00")),
            Quote(id=2, symbol="A", quoted_at=20, price=decimal.Decimal("2.00")),
            Quote(id=3, symbol="B", quoted_at=15, price=decimal.Decimal("5.00")),
        ]
    )
    await Trade.objects.bulk_create(
        [
            Trade(id=1, symbol="A", traded_at=15),
            Trade(id=2, symbol="A", traded_at=25),
            Trade(id=3, symbol="B", traded_at=12),
            Trade(id=4, symbol="A", traded_at=20),
        ]
    )


def priced(source=Quote):
    return Trade.objects.alias(
        quote=AsofJoin(source, on=Q(quoted_at__lte=OuterReference("traded_at"), symbol=OuterReference("symbol")))
    ).order_by("id")


@pytest.mark.asyncio
async def test_each_row_gets_the_closest_row(market):
    queryset = priced()
    assert "ASOF LEFT JOIN" in queryset.values("id", "quote__price").sql()
    assert await queryset.values_list("id", "quote__price", "quote") == [
        (1, decimal.Decimal("1.00"), 1),
        (2, decimal.Decimal("2.00"), 2),
        (3, None, None),
        (4, decimal.Decimal("2.00"), 2),
    ]
    assert await queryset.filter(quote__price__gt=1).values_list("id", flat=True) == [2, 4]
    assert await queryset.filter(quote__isnull=True).values_list("id", flat=True) == [3]
    assert await queryset.filter(quote__price=2).count() == 2


@pytest.mark.asyncio
async def test_a_queryset_joins_its_rows(market):
    cheap = Quote.objects.filter(price__lt=2)
    assert await priced(cheap).values_list("id", "quote__price") == [
        (1, decimal.Decimal("1.00")),
        (2, decimal.Decimal("1.00")),
        (3, None),
        (4, decimal.Decimal("1.00")),
    ]


@pytest.mark.asyncio
async def test_the_join_keeps_a_plan(market):
    def priced_above(price):
        return priced().filter(quote__price__gt=price).values_list("id", flat=True)

    assert await priced_above(1) == [2, 4]
    hits = StatementPlans.hits
    assert await priced_above(0) == [1, 2, 4]
    assert StatementPlans.hits == hits + 1


@pytest.mark.asyncio
async def test_the_condition_takes_equalities_and_one_inequality(market):
    for on in (
        Q(symbol=OuterReference("symbol")),
        Q(quoted_at__lte=OuterReference("traded_at")),
        Q(symbol="A", quoted_at__lte=OuterReference("traded_at")),
        Q(quoted_at__lte=OuterReference("traded_at"), price__gt=OuterReference("id")),
        Q(symbol=OuterReference("symbol")) | Q(quoted_at__lte=OuterReference("traded_at")),
        Q(symbol__startswith=OuterReference("symbol"), quoted_at__lte=OuterReference("traded_at")),
    ):
        with pytest.raises(QueryError, match="AsofJoin"):
            await Trade.objects.alias(quote=AsofJoin(Quote, on=on)).values("quote__price")
    with pytest.raises(QueryError, match="AsofJoin"):
        AsofJoin(Quote.objects.values("id"), on=Q(quoted_at__lte=OuterReference("traded_at")))
