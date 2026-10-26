"""Arithmetic and averages of Decimals on ClickHouse are ClickHouse's own - hare widens nothing: a Decimal128
product wraps around past 38 digits, a Decimal beside a BigInt takes the Decimal's narrow type, a product
past its type's digits is refused by ClickHouse, an average is a Float64."""

import os
from decimal import Decimal

import pytest
import pytest_asyncio

from hare.exceptions import OperationalError
from hare.query.expressions import F, Value, Window
from hare.query.functions import Avg, Concat, Sum
from tests.dialects.clickhouse.models import WideAmount


@pytest_asyncio.fixture
async def amounts(clickhouse_db):
    await WideAmount.objects.create(
        id=1, wide=Decimal("12345678901234567.1234567891"), narrow=Decimal("12345.6789"), count=2**40
    )
    await WideAmount.objects.create(id=2, wide=Decimal("2"), narrow=Decimal("99999.9999"), count=3)
    return WideAmount.objects.order_by("id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (F("wide") * F("wide"), ["462665549914525188.81743240525042213353", "4"]),
        (F("narrow") + F("count"), ["12345.6789", "100002.9999"]),
        (F("narrow") + F("narrow"), ["24691.3578", "199999.9998"]),
    ],
    ids=["wrapped_product", "narrow_integer_sum", "narrow_sum"],
)
async def test_decimal_arithmetic_is_computed_by_clickhouse(amounts, expression, expected):
    assert await amounts.annotate(value=expression).values_list("value", flat=True) == [
        Decimal(item) for item in expected
    ]


@pytest.mark.asyncio
async def test_a_decimal_product_past_its_type_is_refused_by_clickhouse(amounts):
    with pytest.raises(OperationalError, match="Decimal math overflow"):
        await amounts.annotate(value=F("narrow") * F("narrow")).values_list("value", flat=True)


@pytest.mark.asyncio
async def test_a_decimal_average_is_a_float(amounts):
    assert await WideAmount.objects.all().aggregate(average=Avg("wide"), total=Sum(F("wide") * F("wide"))) == {
        "average": pytest.approx(6172839450617284.5),
        "total": Decimal("462665549914525192.81743240525042213353"),
    }
    assert await amounts.annotate(average=Window(Avg("wide"), order_by=["id"])).values_list("average", flat=True) == [
        pytest.approx(12345678901234567.0),
        pytest.approx(6172839450617284.5),
    ]


@pytest.mark.asyncio
async def test_a_decimal_wider_than_its_type_is_read_as_computed(clickhouse_db):
    """A sum outgrowing the 38 digits of its Decimal128 was refused by the field reading it - the server's
    value is read as it is."""
    if os.environ.get("HARE_TEST_DB", "").startswith("clickhouse+clickhouse-connect"):
        pytest.skip("clickhouse-connect rounds a Decimal wider than 38 digits to 38 - the library's own reading")
    widest = Decimal("9999999999999999999999999999.9999999999")
    await WideAmount.objects.create(id=1, wide=widest, narrow=Decimal(0), count=0)
    await WideAmount.objects.create(id=2, wide=Decimal("1.0000000001"), narrow=Decimal(0), count=0)

    assert await WideAmount.objects.all().aggregate(total=Sum("wide")) == {
        "total": Decimal("10000000000000000000000000001.0000000000")
    }
    assert await WideAmount.objects.filter(id=1).annotate(value=F("wide") + Decimal("0.0000000002")).values_list(
        "value", flat=True
    ) == [Decimal("10000000000000000000000000000.0000000001")]


@pytest.mark.asyncio
async def test_a_decimal_literal_is_concatenated_with_every_digit(amounts):
    """A Decimal literal was written as the number ClickHouse read of it - 1.50 as 1.5."""
    values = await amounts.annotate(value=Concat(Value("x"), Value(Decimal("1.50")))).values_list("value", flat=True)
    assert values == ["x1.50", "x1.50"]
