"""Cast, Greatest, Least, NullIf and Collate with the same results on every backend."""

import datetime
from decimal import Decimal
from typing import Any

import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.exceptions import FieldError, OperationalError, QueryError
from hare.query.expressions import F, Value
from hare.query.functions import Cast, Collate, Greatest, Least, NullIf
from hare.transactions.transactions import Transactions
from tests.testmodels import CharFields, ExpressionTypeRow
from tests.utils.database_under_test import DatabaseUnderTest


async def create_row(num: int, fl: float, dec: str, num_null: int | None = None) -> ExpressionTypeRow:
    return await ExpressionTypeRow.objects.create(
        num=num,
        fl=fl,
        dec=Decimal(dec),
        dec3=Decimal("0"),
        d=datetime.date(2024, 1, 2),
        dt=datetime.datetime(2024, 1, 2, 10, 30, tzinfo=datetime.UTC),
        td=datetime.timedelta(0),
        s="x",
        flag=True,
        num_null=num_null,
    )


async def get_value(model: Any, row: Any, expression: Any) -> Any:
    return (await model.objects.filter(id=row.id).annotate(value=expression).values_list("value", flat=True))[0]


@pytest.mark.asyncio
async def test_cast_rounds_and_formats_like_postgres(db):
    row = await create_row(7, 2.5, "2.50")
    other = await create_row(-3, -2.5, "-2.50")

    assert await get_value(ExpressionTypeRow, row, Cast("fl", fields.IntField())) == 2
    assert await get_value(ExpressionTypeRow, other, Cast("fl", fields.IntField())) == -2
    assert await get_value(ExpressionTypeRow, row, Cast("dec", fields.IntField())) == 3
    assert await get_value(ExpressionTypeRow, other, Cast("dec", fields.IntField())) == -3
    assert await get_value(ExpressionTypeRow, row, Cast("num", fields.CharField(max_length=10))) == "7"
    assert await get_value(ExpressionTypeRow, row, Cast("fl", fields.TextField())) == "2.5"
    assert await get_value(ExpressionTypeRow, row, Cast("dec", fields.TextField())) == "2.50"
    assert await get_value(ExpressionTypeRow, row, Cast("flag", fields.TextField())) == "true"
    assert await get_value(
        ExpressionTypeRow, row, Cast("num", fields.DecimalField(max_digits=5, decimal_places=1))
    ) == (Decimal("7.0"))
    assert await get_value(ExpressionTypeRow, row, Cast("dt", fields.DateField())) == datetime.date(2024, 1, 2)
    assert await get_value(ExpressionTypeRow, row, Cast("d", fields.TextField())) == "2024-01-02"
    assert await get_value(ExpressionTypeRow, row, Cast(Value(" 42 "), fields.IntField())) == 42
    assert await ExpressionTypeRow.objects.annotate(whole=Cast("fl", fields.IntField())).filter(whole=2).count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_cast_rejects_what_postgres_rejects(db):
    row = await create_row(40000, 1.0, "9999.99")
    await CharFields.objects.create(id=1, char="abc")

    for expression, model, target in (
        (Cast("char", fields.IntField()), CharFields, 1),
        (Cast("num", fields.SmallIntField()), ExpressionTypeRow, row.id),
        (Cast("dec", fields.DecimalField(max_digits=4, decimal_places=2)), ExpressionTypeRow, row.id),
        (Cast("fl", fields.BooleanField()), ExpressionTypeRow, row.id),
    ):
        with pytest.raises(OperationalError):
            async with Transactions.atomic():
                await model.objects.filter(id=target).annotate(value=expression).values_list("value", flat=True)
    with pytest.raises(FieldError, match="can't convert to UUIDField"):
        Cast("num", fields.UUIDField())


@pytest.mark.asyncio
async def test_greatest_and_least_skip_null(db):
    row = await create_row(7, 2.5, "9.25", num_null=None)
    other = await create_row(1, 8.0, "0.50", num_null=4)

    assert await get_value(ExpressionTypeRow, row, Greatest("num", "num_null")) == 7
    assert await get_value(ExpressionTypeRow, other, Greatest("num", "num_null")) == 4
    assert await get_value(ExpressionTypeRow, other, Least("num", "num_null", 3)) == 1
    assert await get_value(ExpressionTypeRow, row, Greatest("num", "dec")) == Decimal("9.25")
    assert await get_value(ExpressionTypeRow, row, Least("num", "fl")) == 2.5
    assert await get_value(ExpressionTypeRow, row, Greatest("num_null", F("num_null"))) is None
    assert await get_value(ExpressionTypeRow, row, Greatest("d", datetime.date(2030, 1, 1))) == datetime.date(
        2030, 1, 1
    )
    with pytest.raises(QueryError, match="at least two values"):
        Greatest("num")


@pytest.mark.asyncio
async def test_null_if_and_collate(db):
    row = await create_row(0, 1.0, "1.00")
    await CharFields.objects.create(id=1, char="b")
    await CharFields.objects.create(id=2, char="A")

    assert await get_value(ExpressionTypeRow, row, NullIf("num", 0)) is None
    assert await get_value(ExpressionTypeRow, row, NullIf("fl", 2.0)) == 1.0
    collation = "NOCASE" if DatabaseUnderTest.get_engine_name(CharFields._meta.db.dialect) == "sqlite" else "und-x-icu"
    ordered = (
        await CharFields.objects.annotate(sort_key=Collate("char", collation))
        .order_by("sort_key")
        .values_list("char", flat=True)
    )
    assert ordered == ["A", "b"]
    with pytest.raises(QueryError, match="plain name"):
        Collate("char", 'C"; DROP')
