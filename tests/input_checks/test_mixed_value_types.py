"""A filter comparing a field with an expression, and an update setting a field to one, of a type that
doesn't go with the field's - refused before the query runs on every backend, as PostgreSQL refuses
them, instead of comparing or storing text as a number on another backend."""

import datetime
import uuid
from decimal import Decimal

import pytest

from hare import fields
from hare.exceptions import FieldError
from hare.query.expressions import F, Value, Window
from hare.query.functions import (
    Abs,
    Avg,
    Cast,
    Coalesce,
    Count,
    Length,
    Max,
    Min,
    Power,
    Round,
    StdDev,
    Sum,
    Upper,
    window,
)
from tests.shared_models.testmodels import BooleanFields, ExpressionTypeRow, JSONFields, Tournament


async def make_row():
    return await ExpressionTypeRow.objects.create(
        num=7,
        fl=1.5,
        dec=Decimal("2"),
        dec3=Decimal("3"),
        d=datetime.date(2020, 1, 2),
        dt=datetime.datetime(2020, 1, 2, 13, 45, tzinfo=datetime.UTC),
        td=datetime.timedelta(1),
        s="7",
        flag=True,
    )


@pytest.mark.asyncio
async def test_comparisons_of_types_that_go_together(db):
    await make_row()
    assert await ExpressionTypeRow.objects.filter(fl__lt=F("dec")).count() == 1
    assert await ExpressionTypeRow.objects.filter(d__lt=F("dt")).count() == 1
    assert await ExpressionTypeRow.objects.filter(s=Cast(F("num"), fields.CharField(max_length=10))).count() == 1


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"num": F("s")}, r"update\(num=...\) sets a number and text"),
        ({"flag": F("num")}, r"update\(flag=...\) sets a boolean and a number"),
        ({"d": F("dt")}, r"Cannot update 'd' \(date\) from an expression that evaluates to a datetime"),
        ({"dt": F("d")}, r"Cannot update 'dt' \(datetime\) from an expression that evaluates to a date"),
    ],
)
@pytest.mark.asyncio
async def test_setting_a_value_of_a_type_the_field_doesnt_take_is_refused(db, values, message):
    row = await make_row()
    with pytest.raises(FieldError, match=message):
        await ExpressionTypeRow.objects.filter(id=row.id).update(**values)
    assert (await ExpressionTypeRow.objects.get(id=row.id)).d == datetime.date(2020, 1, 2)


@pytest.mark.asyncio
async def test_a_text_field_takes_any_value_as_its_text(db):
    row = await make_row()
    await ExpressionTypeRow.objects.filter(id=row.id).update(s=F("num"))
    await ExpressionTypeRow.objects.filter(id=row.id).update(d=Cast(F("dt"), fields.DateField()))
    assert await ExpressionTypeRow.objects.values_list("s", "d") == [("7", datetime.date(2020, 1, 2))]


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        (F("s") - F("s"), "Cannot use arithmetic on text"),
        (F("s") * 2, "Cannot use arithmetic on text"),
        (F("flag") + F("flag"), "Cannot use arithmetic on a boolean"),
        (Abs("s"), r"Abs\(\) doesn't take text"),
        (Round("s"), r"Round\(\) doesn't take text"),
        (Power(2, "s"), r"Power\(\) doesn't take text"),
        (Length("num"), r"Length\(\) doesn't take a number"),
        (Upper("d"), r"Upper\(\) doesn't take a date or a moment"),
    ],
)
@pytest.mark.asyncio
async def test_arithmetic_and_functions_of_values_they_dont_apply_to_are_refused(db, expression, message):
    await make_row()
    with pytest.raises(FieldError, match=message):
        await ExpressionTypeRow.objects.annotate(value=expression).values_list("value", flat=True)


@pytest.mark.parametrize(
    ("aggregate", "message"),
    [
        (Sum("s"), r"Sum\(\) doesn't take text"),
        (Avg("flag"), r"Avg\(\) doesn't take a boolean"),
        (Sum("d"), r"Sum\(\) doesn't take a date or a moment"),
        (StdDev("td"), r"StdDev\(\) doesn't take a duration"),
    ],
)
@pytest.mark.asyncio
async def test_aggregates_of_values_they_dont_apply_to_are_refused(db, aggregate, message):
    await make_row()
    with pytest.raises(FieldError, match=message):
        await ExpressionTypeRow.objects.aggregate(value=aggregate)


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        (Upper("data__k"), r"Upper\(\) doesn't take a JSON value"),
        (Length("data"), r"Length\(\) doesn't take a JSON value"),
        (Abs("data__n"), r"Abs\(\) doesn't take a JSON value"),
        (Round(F("data__n")), r"Round\(\) doesn't take a JSON value"),
        (Sum("data__n"), r"Sum\(\) doesn't take a JSON value"),
    ],
)
@pytest.mark.asyncio
async def test_functions_of_text_and_numbers_refuse_a_json_value(db, expression, message):
    """PostgreSQL refused them after the query ran (upper(jsonb) does not exist), SQLite ran them over the JSON
    text (a key's text read with its quotes, "ABC" as '"ABC"'), ClickHouse refused or read the quotes too."""
    await JSONFields.objects.create(id=1, data={"k": "abc", "n": -2})
    with pytest.raises(FieldError, match=message):
        await JSONFields.objects.annotate(value=expression).values_list("value", flat=True)
    cast_key = Cast("data__n", fields.IntField())
    assert await JSONFields.objects.annotate(value=Abs(cast_key)).values_list("value", flat=True) == [2]


@pytest.mark.asyncio
async def test_aggregates_and_functions_of_values_they_apply_to(db):
    await make_row()
    assert await ExpressionTypeRow.objects.aggregate(
        total=Sum("td"),
        longest=Max("s"),
        first=Min("d"),
        size=Max(Length("s")),
        number=Sum(Cast("flag", fields.IntField())),
    ) == {
        "total": datetime.timedelta(1),
        "longest": "7",
        "first": datetime.date(2020, 1, 2),
        "size": 1,
        "number": 1,
    }


@pytest.mark.parametrize(
    ("aggregate", "message"),
    [
        (window.Sum("s"), r"Sum\(\) doesn't take text"),
        (window.Avg("flag"), r"Avg\(\) doesn't take a boolean"),
    ],
)
@pytest.mark.asyncio
async def test_a_window_aggregate_of_values_it_doesnt_apply_to_is_refused(db, aggregate, message):
    await make_row()
    with pytest.raises(FieldError, match=message):
        await ExpressionTypeRow.objects.annotate(value=Window(aggregate)).values_list("value", flat=True)


@pytest.mark.asyncio
async def test_a_text_lookup_reads_a_boolean_as_its_databases_text(db):
    """PostgreSQL's true/false, SQLite's 1/0."""
    await BooleanFields.objects.create(id=1, boolean=True)
    await BooleanFields.objects.create(id=2, boolean=False)
    true_text, false_text = ("1", "0") if BooleanFields._meta.connection.dialect.name == "sqlite" else ("t", "fal")
    assert await BooleanFields.objects.filter(boolean__icontains=true_text).values_list("id", flat=True) == [1]
    assert await BooleanFields.objects.filter(boolean__startswith=false_text).values_list("id", flat=True) == [2]


@pytest.mark.asyncio
async def test_a_transformed_value_compares_with_a_value_of_its_own_type(db):
    row = await make_row()
    await ExpressionTypeRow.objects.filter(id=row.id).update(num=2020)
    assert await ExpressionTypeRow.objects.filter(dt__year=F("num")).count() == 1


@pytest.mark.parametrize(
    "value",
    [
        1.5,
        Decimal("1.50"),
        2**63 - 1,
        datetime.datetime(2020, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        datetime.time(12, 30, tzinfo=datetime.UTC),
        datetime.timedelta(days=1, seconds=2, microseconds=3),
    ],
    ids=["float", "decimal", "int64", "datetime", "time", "timedelta"],
)
@pytest.mark.asyncio
async def test_a_coalesce_of_literals_alone_keeps_the_value(db, value):
    """Nothing typed COALESCE($1, $2) - PostgreSQL read both parameters as text, which asyncpg refused
    to bind a number or a moment against, and rust_pg read back as text."""
    tournament = await Tournament.objects.create(name="T")
    result = await (
        Tournament.objects.filter(id=tournament.id)
        .annotate(value=Coalesce(Value(None), Value(value)))
        .values_list("value", flat=True)
    )
    assert result == [value]


@pytest.mark.parametrize("value", [uuid.UUID("00000000-0000-0000-0000-000000000001"), {"k": 1}], ids=["uuid", "dict"])
@pytest.mark.asyncio
async def test_a_filter_of_a_literal_annotation_binds_the_value_as_the_literal(db, value):
    """The literal was bound in its stored form, the value compared with it as it was - a UUID or a
    dict no driver binds."""
    await Tournament.objects.create(name="T")
    assert await Tournament.objects.annotate(literal=Value(value)).filter(literal=value).count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (Count("id"), "1"),
        (Count("id"), 1.5),
        (Sum("id"), "x"),
        (F("id") + 1, "4"),
        (Upper("name"), 5),
    ],
    ids=["count_text", "count_fraction", "sum_text", "arithmetic_text", "text_number"],
)
async def test_a_filter_on_an_annotation_takes_the_values_its_field_takes(db, annotation, value):
    """The value of a filter on an annotation was bound as it was - SQLite compared a text with a number
    and matched nothing, asyncpg refused it, rust_pg read it as the number; a field's own filter refuses
    it."""
    from hare.exceptions import ValidationError

    await Tournament.objects.create(id=1, name="a")
    with pytest.raises(ValidationError, match="expected"):
        await Tournament.objects.annotate(value=annotation).filter(value=value).count()
