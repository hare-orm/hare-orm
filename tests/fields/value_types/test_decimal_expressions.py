"""Expressions of Decimals with more digits than a double holds keep every digit on every database -
SQLite worked them out in doubles, ~15 significant digits, and an update wrote the rounded value."""

from decimal import Context, Decimal

import pytest
import pytest_asyncio

from hare.query.expressions import Case, F, Q, Value, When, Window
from hare.query.functions import Abs, Avg, Ceil, Coalesce, Concat, Floor, Greatest, Mod, Round, StdDev, Sum
from tests.shared_models.testmodels import HighPrecisionDecimalFields

BIG = Decimal("12345678901234567890.123456789012345678")
OTHER = Decimal("-98765432109876543210.987654321098765432")


@pytest_asyncio.fixture
async def rows(db):
    await HighPrecisionDecimalFields.objects.create(big=BIG)
    await HighPrecisionDecimalFields.objects.create(big=OTHER)
    return HighPrecisionDecimalFields.objects.order_by("id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (F("big") + 1, ["12345678901234567891.123456789012345678", "-98765432109876543209.987654321098765432"]),
        (
            F("big") - Decimal("0.000000000000000001"),
            ["12345678901234567890.123456789012345677", "-98765432109876543210.987654321098765433"],
        ),
        (F("big") * 3, ["37037036703703703670.370370367037037034", "-296296296329629629632.962962963296296296"]),
        (-F("big"), ["-12345678901234567890.123456789012345678", "98765432109876543210.987654321098765432"]),
        (Abs("big"), ["12345678901234567890.123456789012345678", "98765432109876543210.987654321098765432"]),
        (Ceil("big"), ["12345678901234567891", "-98765432109876543210"]),
        (Floor("big"), ["12345678901234567890", "-98765432109876543211"]),
        (Mod("big", 7), ["1.123456789012345678", "-3.987654321098765432"]),
        (Coalesce("big", Value(Decimal(0))), [str(BIG), str(OTHER)]),
        (Case(When(Q(big__gt=0), then=F("big")), default=Value(Decimal("1.5"))), [str(BIG), "1.5"]),
        (
            Window(Sum("big"), order_by=["id"]),
            ["12345678901234567890.123456789012345678", "-86419753208641975320.864197532086419754"],
        ),
    ],
    ids=[
        "add",
        "subtract",
        "multiply",
        "negate",
        "abs",
        "ceil",
        "floor",
        "mod",
        "coalesce",
        "case",
        "window",
    ],
)
async def test_a_decimal_expression_keeps_every_digit(rows, expression, expected):
    assert await rows.annotate(value=expression).values_list("value", flat=True) == [
        Decimal(item) for item in expected
    ]


@pytest.mark.asyncio
async def test_a_decimal_quotient_keeps_every_digit_it_shows(rows):
    """Each database shows a quotient at its own places - the digits it shows are the quotient's."""
    quotients = await rows.annotate(value=F("big") / 7).values_list("value", flat=True)
    for quotient, dividend in zip(quotients, (BIG, OTHER), strict=True):
        exact = Context(prec=80).divide(dividend, 7)
        assert abs(quotient - exact) < Decimal("1e-18")


@pytest.mark.asyncio
async def test_a_decimal_aggregate_keeps_every_digit(rows):
    assert await HighPrecisionDecimalFields.objects.all().aggregate(
        total=Sum("big"), average=Avg("big"), deviation=StdDev("big")
    ) == {
        "total": Decimal("-86419753208641975320.864197532086419754"),
        "average": Decimal("-43209876604320987660.432098766043209877"),
        "deviation": Decimal("55555555505555555550.555555555055555555"),
    }


@pytest.mark.asyncio
async def test_an_update_by_a_decimal_expression_writes_every_digit(rows):
    step = Decimal("0.000000000000000001")
    stepped = Decimal("12345678901234567890.123456789012345679")
    await HighPrecisionDecimalFields.objects.filter(big=BIG).update(big=F("big") + step)
    assert await HighPrecisionDecimalFields.objects.filter(big=stepped).count() == 1
    assert await rows.annotate(next=F("big") + step).filter(next__gt=stepped).count() == 1
    assert await HighPrecisionDecimalFields.objects.annotate(doubled=F("big") * 2).order_by("doubled").values_list(
        "big", flat=True
    ) == [OTHER, stepped]


@pytest.mark.asyncio
async def test_a_round_to_more_places_than_the_default_context_holds(rows):
    """SQLite's exact Round() quantized in the default context of 28 digits - more failed in its UDF."""
    values = await rows.annotate(value=Round("big", 30)).values_list("value", flat=True)
    assert [str(value) for value in values] == [
        "12345678901234567890.123456789012345678000000000000",
        "-98765432109876543210.987654321098765432000000000000",
    ]


@pytest.mark.asyncio
async def test_the_greatest_of_a_decimal_and_a_float_is_a_float(rows):
    """SQLite gave the Decimal's text for the float result - reading it raised ValidationError."""
    values = await rows.annotate(value=Greatest("big", Value(1.5))).values_list("value", flat=True)
    assert values == [float(BIG), 1.5]


@pytest.mark.asyncio
async def test_a_remainder_by_a_decimal_nan_is_nan(rows):
    """SQLite took Mod() of a NaN for a float remainder and returned the text 'NaN'; the % operator cast the
    NaN to a number, 0, and returned NULL."""
    if rows.model._meta.connection.dialect.name == "clickhouse":
        pytest.skip("a ClickHouse Decimal holds no NaN")
    for expression in (Mod("big", Value(Decimal("NaN"))), F("big") % Value(Decimal("NaN"))):
        values = await rows.annotate(value=expression).values_list("value", flat=True)
        assert all(isinstance(value, Decimal) and value.is_nan() for value in values)


@pytest.mark.asyncio
async def test_a_decimal_literal_is_concatenated_with_every_digit(rows):
    """ClickHouse wrote a Decimal literal as the number it read of it - 1.50 as 1.5."""
    values = await rows.annotate(value=Concat(Value("x"), Value(Decimal("1.50")))).values_list("value", flat=True)
    assert values == ["x1.50", "x1.50"]
