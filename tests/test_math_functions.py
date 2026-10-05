"""Math functions: values, result types, literals in any position, NULL and domain errors."""

import datetime
import math
from decimal import Decimal
from typing import Any

import pytest

from hare.contrib.test import requires_features
from hare.dialects.sqlite.functions.sqlite_math_functions import SqliteMathFunctions
from hare.exceptions import OperationalError, QueryError
from hare.query.expressions import F, Value
from hare.query.functions import (
    Abs,
    ACos,
    ASin,
    ATan,
    ATan2,
    Ceil,
    Coalesce,
    Cos,
    Cot,
    Degrees,
    Exp,
    Floor,
    Ln,
    Log,
    Mod,
    Pi,
    Power,
    Radians,
    Sign,
    Sin,
    Sqrt,
    Tan,
)
from hare.transactions.transactions import Transactions
from tests.testmodels import ExpressionTypeRow


async def create_row(num: int, fl: float, dec: str, num_null: int | None = None) -> ExpressionTypeRow:
    return await ExpressionTypeRow.objects.create(
        num=num,
        fl=fl,
        dec=Decimal(dec),
        dec3=Decimal("0"),
        d=datetime.date(2020, 1, 1),
        dt=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC),
        td=datetime.timedelta(0),
        s="x",
        flag=True,
        num_null=num_null,
    )


async def get_value(row: ExpressionTypeRow, expression: Any) -> Any:
    return (
        await ExpressionTypeRow.objects.filter(id=row.id).annotate(value=expression).values_list("value", flat=True)
    )[0]


@pytest.mark.asyncio
async def test_rounding_functions_keep_the_argument_type(db):
    row = await create_row(-7, -2.5, "-1.25")

    assert (await get_value(row, Abs("num")), await get_value(row, Abs("fl")), await get_value(row, Abs("dec"))) == (
        7,
        2.5,
        Decimal("1.25"),
    )
    assert await get_value(row, Ceil("fl")) == -2.0
    assert await get_value(row, Floor("fl")) == -3.0
    assert await get_value(row, Ceil("dec")) == Decimal("-1.00")
    assert await get_value(row, Floor("dec")) == Decimal("-2.00")
    assert await get_value(row, Ceil("num")) == -7
    assert (await get_value(row, Sign("num")), await get_value(row, Sign("fl"))) == (-1, -1.0)
    assert isinstance(await get_value(row, Abs("num")), int)
    assert isinstance(await get_value(row, Floor("dec")), Decimal)


@pytest.mark.asyncio
async def test_a_float_rounded_to_zero_keeps_its_sign(db):
    """PostgreSQL's ceil(-0.5) is -0; SQLite's functions gave 0."""
    row = await create_row(0, -0.5, "0")
    assert math.copysign(1, await get_value(row, Ceil("fl"))) == -1
    assert await get_value(row, Ceil("fl")) == 0
    assert math.copysign(1, await get_value(row, Floor(Value(0.5)))) == 1
    assert math.copysign(1, SqliteMathFunctions.ceiling(-0.5)) == -1


@pytest.mark.asyncio
async def test_mod_takes_the_dividend_sign_and_keeps_integers(db):
    row = await create_row(-7, 7.5, "2.50")

    assert await get_value(row, Mod("num", 3)) == -1
    assert isinstance(await get_value(row, Mod("num", 3)), int)
    assert await get_value(row, Mod(7, "num")) == 0
    assert await get_value(row, Mod("fl", 2)) == pytest.approx(1.5)
    assert await get_value(row, Mod("dec", Decimal("0.75"))) == Decimal("0.25")
    with pytest.raises(OperationalError):
        await get_value(row, Mod("num", 0))


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_exponential_and_logarithmic_functions(db):
    row = await create_row(8, 0.5, "2.25")

    assert await get_value(row, Sqrt("num")) == pytest.approx(math.sqrt(8))
    assert await get_value(row, Sqrt("dec")) == pytest.approx(Decimal("1.5"))
    assert isinstance(await get_value(row, Sqrt("dec")), Decimal)
    assert await get_value(row, Power("num", 2)) == pytest.approx(64.0)
    assert await get_value(row, Power(2, "fl")) == pytest.approx(math.sqrt(2))
    assert await get_value(row, Exp("fl")) == pytest.approx(math.exp(0.5))
    assert await get_value(row, Ln("num")) == pytest.approx(math.log(8))
    assert await get_value(row, Log(2, "num")) == pytest.approx(3.0)
    # Each failing query in its own savepoint - a Postgres error aborts the surrounding transaction.
    with pytest.raises(OperationalError):
        async with Transactions.atomic():
            await get_value(row, Ln(F("num") - 8))
    with pytest.raises(OperationalError):
        async with Transactions.atomic():
            await get_value(row, Sqrt(F("num") * -1))


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_refused_argument_gives_the_postgres_message(db):
    """SQLite reported every error of a function as 'user-defined function raised exception', an
    overflow as 'string or blob too big'."""
    row = await create_row(-1, -2.0, "0.00")

    for expression, message in (
        (Sqrt("num"), "cannot take square root of a negative number"),
        (Ln(F("num") + 1), "cannot take logarithm of zero"),
        (Ln("fl"), "cannot take logarithm of a negative number"),
        (Log(2, "num"), "cannot take logarithm of a negative number"),
        (Power(10.0, 400), "value out of range: overflow"),
        (Exp(1000), "value out of range: overflow"),
        (ASin(F("num") * 2), "input is out of range"),
    ):
        with pytest.raises(OperationalError, match=message):
            async with Transactions.atomic():
                await get_value(row, expression)


@pytest.mark.asyncio
async def test_trigonometric_functions_are_floats(db):
    row = await create_row(1, 0.5, "1.00")

    for function, expected in (
        (Sin("fl"), math.sin(0.5)),
        (Cos("fl"), math.cos(0.5)),
        (Tan("fl"), math.tan(0.5)),
        (Cot("fl"), 1 / math.tan(0.5)),
        (ASin("fl"), math.asin(0.5)),
        (ACos("fl"), math.acos(0.5)),
        (ATan("dec"), math.atan(1)),
        (ATan2("num", "fl"), math.atan2(1, 0.5)),
        (Degrees(Pi()), 180.0),
        (Radians(180), math.pi),
    ):
        value = await get_value(row, function)
        assert isinstance(value, float)
        assert value == pytest.approx(expected)
    with pytest.raises(OperationalError):
        await get_value(row, ASin(F("num") * 2))


@pytest.mark.asyncio
async def test_math_function_in_a_filter_and_with_null(db):
    first = await create_row(-9, 1.0, "4.00", num_null=None)
    second = await create_row(4, 1.0, "1.00", num_null=16)

    assert await ExpressionTypeRow.objects.annotate(size=Abs("num")).filter(size__gt=5).values_list(
        "id", flat=True
    ) == [first.id]
    assert await get_value(first, Sqrt("num_null")) is None
    assert await get_value(second, Sqrt("num_null")) == pytest.approx(4.0)


def test_math_function_rejects_a_wrong_argument():
    with pytest.raises(QueryError, match="takes 2 argument"):
        Mod("num")
    with pytest.raises(QueryError, match="takes numbers"):
        Abs(True)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_division_by_zero_raises(db):
    row = await create_row(3, 1.5, "2.50")

    for expression in (
        F("num") / (F("num") - 3),
        F("fl") / 0.0,
        F("dec") / Decimal("0.00"),
        F("dec") / (F("dec") - Decimal("2.5")),
        F("num") / Value(0),
    ):
        with pytest.raises(OperationalError):
            async with Transactions.atomic():
                await get_value(row, expression)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_zero_divisor_raises_after_the_same_query_with_a_non_zero_one(db):
    row = await create_row(4, 1.5, "2.50")

    for make_expression in (
        lambda divisor: F("num") / divisor,
        lambda divisor: F("num") % divisor,
        lambda divisor: Mod("num", divisor),
    ):
        assert await get_value(row, make_expression(2)) in {0, 2}
        with pytest.raises(OperationalError):
            async with Transactions.atomic():
                await get_value(row, make_expression(0))


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_remainder_by_zero_raises_and_writes_nothing(db):
    row = await create_row(3, 1.5, "2.50")

    for expression in (
        F("num") % 0,
        F("num") % (F("num") - 3),
        F("fl") % 0.0,
        F("dec") % Decimal("0.00"),
        F("dec") % 0,
        Mod("num", 0),
        Mod("fl", 0),
        Mod("dec", F("num") - 3),
    ):
        with pytest.raises(OperationalError):
            async with Transactions.atomic():
                await get_value(row, expression)
    with pytest.raises(OperationalError):
        async with Transactions.atomic():
            await ExpressionTypeRow.objects.filter(id=row.id).update(num=F("num") % 0)
    with pytest.raises(OperationalError):
        async with Transactions.atomic():
            await ExpressionTypeRow.objects.filter(num=F("num") % 0).count()
    assert await ExpressionTypeRow.objects.filter(id=row.id).values_list("num", flat=True) == [3]
    assert await get_value(row, F("num_null") % 0) is None


@pytest.mark.asyncio
async def test_a_null_dividend_or_divisor_divides_into_null(db):
    row = await create_row(3, 1.5, "2.50", num_null=None)

    assert await get_value(row, F("num_null") / 0) is None
    assert await get_value(row, F("num_null") / (F("num") - 3)) is None
    assert await get_value(row, F("num") / F("num_null")) is None
    assert await get_value(row, F("fl") / F("num")) == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_mod_of_a_decimal_is_exact_at_the_larger_scale(db):
    row = await create_row(1, 1.0, "12.34")

    assert str(await get_value(row, Mod("dec", 7))) == "5.34"
    assert str(await get_value(row, Mod("dec", Decimal("0.1")))) == "0.04"
    assert str(await get_value(row, Mod("dec", Decimal("0.125")))) == "0.090"
    assert str(await get_value(row, Mod("num", Decimal("0.3")))) == "0.1"
    assert await get_value(row, Mod("dec", "dec")) == Decimal("0")


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_result_too_small_for_a_float_raises(db):
    row = await create_row(1, -1000.0, "1.00")

    with pytest.raises(OperationalError):
        async with Transactions.atomic():
            await get_value(row, Exp("fl"))
    with pytest.raises(OperationalError):
        async with Transactions.atomic():
            await get_value(row, Power(10, "fl"))
    assert await get_value(row, Power(0, 2)) == 0.0
    assert await get_value(row, Exp(F("fl") * 0)) == 1.0


@pytest.mark.asyncio
async def test_a_decimal_column_is_compared_with_another_numbers_expression_by_value(db):
    cheap = await create_row(100, 1.5, "0.01")
    dear = await create_row(3, 0.5, "10.50")
    huge = await create_row(7, -1.5, "99999999.99")

    async def get_ids(**filters: Any) -> list[int]:
        return await ExpressionTypeRow.objects.filter(**filters).order_by("id").values_list("id", flat=True)

    # 10.50 is no less than 9, whatever their texts compare as.
    assert await get_ids(dec__lt=F("num") * 3) == [cheap.id]
    assert await get_ids(dec__gt=F("num") * 3) == [dear.id, huge.id]
    assert await get_ids(dec__lte=Abs("num")) == [cheap.id]
    assert await get_ids(dec__gte=F("num") + F("fl")) == [dear.id, huge.id]
    assert await get_ids(dec=F("num") * 3.5) == [dear.id]
    excluded = await ExpressionTypeRow.objects.exclude(dec__lt=F("num")).order_by("id").values_list("id", flat=True)
    assert excluded == [dear.id, huge.id]


@pytest.mark.asyncio
async def test_an_integer_literal_beyond_32_bits_meets_an_integer_column(db):
    row = await create_row(3, 1.5, "2.50", num_null=None)
    assert await get_value(row, F("num") * 10**12) == 3 * 10**12
    assert await get_value(row, F("num") + 3_000_000_000) == 3_000_000_003
    assert await get_value(row, Coalesce("num_null", 3_000_000_000)) == 3_000_000_000
    # The same query with a literal of 32 bits, and beyond again.
    assert await get_value(row, F("num") * 2) == 6
    assert await get_value(row, F("num") * 2**40) == 3 * 2**40


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
async def test_an_integer_result_out_of_its_type_range_is_refused(db):
    """A result beyond its type - an integer column's 32 bits, a literal's 64 - raises, as PostgreSQL's
    "integer out of range"; neither widened (ClickHouse, SQLite) nor wrapped around (ClickHouse)."""
    row = await create_row(3, 1.5, "2.50")
    with pytest.raises(OperationalError, match="integer out of range"):
        async with Transactions.atomic():
            await get_value(row, F("num") + 2_147_483_647)
    with pytest.raises(OperationalError, match="bigint out of range"):
        async with Transactions.atomic():
            await get_value(row, F("num") * 4 * 10**18)
    assert await get_value(row, F("num") + 2_147_483_644) == 2_147_483_647
