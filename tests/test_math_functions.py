"""Math functions: values, result types, literals in any position, NULL and domain errors."""

import datetime
import math
from decimal import Decimal
from typing import Any

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import OperationalError, QueryError
from hare.query.expressions import F
from hare.query.functions import (
    Abs,
    ACos,
    ASin,
    ATan,
    ATan2,
    Ceil,
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
