"""Arithmetic and averages of Decimals on ClickHouse give PostgreSQL's values - a Decimal128 product wrapped
around past 38 digits without an error, a Decimal beside a BigInt took the Decimal's narrow type, an
average was a Float64, and clickhouse-driver rounded a result to its type's digits."""

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
        (F("wide") * F("wide"), ["152415787532388348574912508489560.35073921817488187881", "4"]),
        (F("narrow") + F("count"), ["1099511640121.6789", "100002.9999"]),
        (F("narrow") * F("count"), ["13574217503338817.1264", "299999.9997"]),
        (F("narrow") + F("narrow"), ["24691.3578", "199999.9998"]),
        (F("narrow") * F("narrow"), ["152415787.50190521", "9999999980.00000001"]),
        (F("wide") - F("narrow") * F("narrow"), ["12345678748818779.6215515791", "-9999999978.00000001"]),
    ],
    ids=["product", "integer_sum", "integer_product", "narrow_sum", "narrow_product", "difference"],
)
async def test_decimal_arithmetic_keeps_every_digit(amounts, expression, expected):
    assert await amounts.annotate(value=expression).values_list("value", flat=True) == [
        Decimal(item) for item in expected
    ]


@pytest.mark.asyncio
async def test_a_decimal_average_is_an_exact_decimal(amounts):
    assert await WideAmount.objects.all().aggregate(average=Avg("wide"), total=Sum(F("wide") * F("wide"))) == {
        "average": Decimal("6172839450617284.56172839455"),
        "total": Decimal("152415787532388348574912508489564.35073921817488187881"),
    }
    assert await amounts.annotate(average=Window(Avg("wide"), order_by=["id"])).values_list("average", flat=True) == [
        Decimal("12345678901234567.1234567891"),
        Decimal("6172839450617284.56172839455"),
    ]


@pytest.mark.asyncio
async def test_a_decimal_product_past_the_digits_clickhouse_holds_is_refused(amounts):
    """A Decimal256 wraps around past its 76 digits - the product came back as another number."""
    await WideAmount.objects.filter(id=1).update(wide=Decimal("1" + "0" * 27))
    with pytest.raises(OperationalError, match="more digits than the decimal holds"):
        await WideAmount.objects.annotate(value=F("wide") * F("wide") * F("wide")).values_list("value", flat=True)


@pytest.mark.asyncio
async def test_an_average_keeps_the_scale_of_its_type_on_both_drivers(amounts):
    """clickhouse-driver divided each value by its scale, which dropped the zeros it ended with - an average
    read 12345.6789 over TCP and 12345.67890000000000000000 over HTTP."""
    result = await WideAmount.objects.filter(id=1).aggregate(value=Avg("narrow"))
    assert str(result["value"]) == "12345.67890000000000000000"


@pytest.mark.asyncio
async def test_a_decimal_literal_is_concatenated_with_every_digit(amounts):
    """A Decimal literal was written as the number ClickHouse read of it - 1.50 as 1.5."""
    values = await amounts.annotate(value=Concat(Value("x"), Value(Decimal("1.50")))).values_list("value", flat=True)
    assert values == ["x1.50", "x1.50"]
