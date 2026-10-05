"""Cast, Greatest, Least, NullIf and Collate with the same results on every backend."""

import datetime
from decimal import Decimal
from typing import Any

import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.exceptions import FieldError, OperationalError, QueryError
from hare.query.expressions import Case, F, Value, When
from hare.query.functions import Cast, Coalesce, Collate, Concat, Greatest, Least, NullIf
from hare.transactions.transactions import Transactions
from tests.testmodels import CharFields, ExpressionTypeRow, JSONFields
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


@pytest.mark.asyncio
async def test_json_becomes_the_text_postgres_writes_of_it(db):
    """SQLite wrote the stored text - no spaces, keys in the order written, ``1e+20``."""
    await JSONFields.objects.create(id=1, data={"bb": [1, 2.5, None, True], "n": 1e20, "a": 0})
    texts = (
        await JSONFields.objects.annotate(
            text=Cast("data", fields.TextField()),
            joined=Concat("data", Value("|")),
            path_text=Cast(F("data__bb"), fields.TextField()),
            scalar_text=Concat(F("data__bb__1"), Value("|")),
        )
        .values_list("text", "joined", "path_text", "scalar_text")
        .get(id=1)
    )
    text = '{"a": 0, "n": 100000000000000000000, "bb": [1, 2.5, null, true]}'
    assert texts == (text, f"{text}|", "[1, 2.5, null, true]", "2.5|")


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
async def test_cast_reads_text_as_postgres_input(db):
    row = await create_row(1, 1.0, "1.00")

    for text, expected in ((" on ", True), ("of", False), ("tr", True), ("N", False), ("1", True)):
        assert await get_value(ExpressionTypeRow, row, Cast(Value(text), fields.BooleanField())) is expected
    # A date is the date its text writes - its time and offset ignored; a moment is taken at its offset.
    assert await get_value(
        ExpressionTypeRow, row, Cast(Value("2024-02-29 23:30:00-02:00"), fields.DateField())
    ) == datetime.date(2024, 2, 29)
    assert await get_value(
        ExpressionTypeRow, row, Cast(Value("2024-02-29 23:30:00-02:00"), fields.DatetimeField())
    ) == datetime.datetime(2024, 3, 1, 1, 30, tzinfo=datetime.UTC)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_cast_of_text_rejects_what_postgres_rejects(db):
    row = await create_row(1, 1.0, "1.00")

    for expression in (
        Cast(Value("o"), fields.BooleanField()),
        Cast(Value(""), fields.BooleanField()),
        Cast(Value("2023-02-29"), fields.DateField()),
        Cast(Value("4.2"), fields.IntField()),
        Cast(Value("abc"), fields.DecimalField(max_digits=5, decimal_places=1)),
    ):
        with pytest.raises(OperationalError):
            async with Transactions.atomic():
                await get_value(ExpressionTypeRow, row, expression)


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
    collation = (
        "NOCASE" if DatabaseUnderTest.get_engine_name(CharFields._meta.connection.dialect) == "sqlite" else "und-x-icu"
    )
    ordered = (
        await CharFields.objects.annotate(sort_key=Collate("char", collation))
        .order_by("sort_key")
        .values_list("char", flat=True)
    )
    assert ordered == ["A", "b"]
    with pytest.raises(QueryError, match="plain name"):
        Collate("char", 'C"; DROP')


@pytest.mark.asyncio
async def test_null_if_compares_a_decimal_as_a_number(db):
    row = await create_row(7, 2.5, "0.00")

    for expression in (
        NullIf("dec", 0),
        NullIf("dec", Value(Decimal("0.0"))),
        NullIf("num", Value(Decimal("7.0"))),
        NullIf("fl", Value(Decimal("2.5"))),
        NullIf("dec", F("dec3")),
    ):
        assert await get_value(ExpressionTypeRow, row, expression) is None
    assert await get_value(ExpressionTypeRow, row, NullIf("dec", Value(Decimal("1")))) == Decimal("0.00")
    assert await get_value(ExpressionTypeRow, row, NullIf("num", Value(Decimal("7.5")))) == 7


@pytest.mark.asyncio
async def test_case_and_coalesce_of_decimals_of_different_scales(db):
    row = await create_row(7, 2.5, "1.25")

    assert str(await get_value(ExpressionTypeRow, row, Coalesce("dec_null", F("dec3")))) == "0.000"
    assert str(await get_value(ExpressionTypeRow, row, Coalesce("dec_null", Value(Decimal("0.125"))))) == "0.125"
    case = Case(When(flag=True, then=F("dec")), default=F("dec3"))
    assert str(await get_value(ExpressionTypeRow, row, case)) == "1.250"
    case = Case(When(num__gt=10, then=Value(Decimal("1.5"))), default=Value(Decimal("0.25")))
    assert str(await get_value(ExpressionTypeRow, row, case)) == "0.25"


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        (Coalesce("num_null", Value("x")), r"Coalesce\(\) combines a number and text"),
        (Coalesce("num_null", Value("1.5")), r"Coalesce\(\) combines a number and text"),
        (Coalesce("num", F("s")), r"Coalesce\(\) combines a number and text"),
        (Case(When(num__gt=1, then=F("s")), default=Value(1)), r"Case\(\) combines text and a number"),
        (Case(When(num__gt=1, then=F("flag")), default=F("num")), r"Case\(\) combines a boolean and a number"),
        (Case(When(num__gt=1, then=F("d")), default=F("td")), r"Case\(\) combines a date or a moment and a duration"),
        (NullIf("num", Value("x")), r"NullIf\(\) combines a number and text"),
        (Greatest("num", Value("x")), r"Greatest\(\) combines a number and text"),
    ],
)
@pytest.mark.asyncio
async def test_values_of_types_that_dont_combine_are_refused(db, expression, message):
    with pytest.raises(FieldError, match=message):
        await ExpressionTypeRow.objects.annotate(value=expression).values_list("value", flat=True)


@pytest.mark.asyncio
async def test_values_of_one_type_combine(db):
    await ExpressionTypeRow.objects.create(
        num=1,
        fl=1.5,
        dec=Decimal("2"),
        dec3=Decimal("3"),
        d=datetime.date(2020, 1, 2),
        dt=datetime.datetime(2020, 1, 2, 13, 45, tzinfo=datetime.UTC),
        td=datetime.timedelta(1),
        s="s",
        flag=True,
    )
    rows = await ExpressionTypeRow.objects.annotate(
        number=Coalesce("num_null", F("fl")),
        moment=Case(When(num__gt=5, then=F("d")), default=F("dt")),
        text=Coalesce(Value(None), F("s"), Value("t")),
    ).values_list("number", "moment", "text")
    assert rows == [(1.5, datetime.datetime(2020, 1, 2, 13, 45, tzinfo=datetime.UTC), "s")]


@pytest.mark.asyncio
async def test_a_date_beside_a_moment_is_its_midnight_in_utc(db):
    await ExpressionTypeRow.objects.create(
        num=1,
        fl=1.5,
        dec=Decimal("2"),
        dec3=Decimal("3"),
        d=datetime.date(2020, 1, 2),
        dt=datetime.datetime(2020, 1, 2, 13, 45, tzinfo=datetime.UTC),
        td=datetime.timedelta(1),
        s="s",
        flag=True,
    )
    rows = await ExpressionTypeRow.objects.annotate(
        value=Coalesce(Value(None), F("dt"), Value(datetime.date(2001, 2, 3))),
        fallback=Coalesce(NullIf("dt", F("dt")), Value(datetime.date(2001, 2, 3))),
    ).values_list("value", "fallback")
    assert rows == [
        (
            datetime.datetime(2020, 1, 2, 13, 45, tzinfo=datetime.UTC),
            datetime.datetime(2001, 2, 3, tzinfo=datetime.UTC),
        )
    ]
