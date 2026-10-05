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
from tests.testmodels import BooleanFields, Event, ExpressionTypeRow, Tournament


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


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ({"s": F("num")}, r"filter\(s=...\) compares text and a number"),
        ({"num__gt": F("s")}, r"filter\(num__gt=...\) compares a number and text"),
        ({"flag": F("num")}, r"filter\(flag=...\) compares a boolean and a number"),
        ({"d__lte": F("num")}, r"filter\(d__lte=...\) compares a date or a moment and a number"),
    ],
)
@pytest.mark.asyncio
async def test_a_comparison_of_types_that_dont_go_together_is_refused(db, condition, message):
    await make_row()
    with pytest.raises(FieldError, match=message):
        await ExpressionTypeRow.objects.filter(**condition).count()


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
        (Max("flag"), r"Max\(\) doesn't take a boolean"),
        (Min("flag"), r"Min\(\) doesn't take a boolean"),
    ],
)
@pytest.mark.asyncio
async def test_aggregates_of_values_they_dont_apply_to_are_refused(db, aggregate, message):
    await make_row()
    with pytest.raises(FieldError, match=message):
        await ExpressionTypeRow.objects.aggregate(value=aggregate)


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
    ("model", "condition", "message"),
    [
        (Event, {"tournament__name": F("event_id")}, r"filter\(tournament__name=...\) compares text and a number"),
        (Tournament, {"events__name": F("id")}, r"filter\(events__name=...\) compares text and a number"),
    ],
)
@pytest.mark.asyncio
async def test_a_comparison_across_a_relation_is_checked_too(db, model, condition, message):
    with pytest.raises(FieldError, match=message):
        await model.objects.filter(**condition).count()


@pytest.mark.asyncio
async def test_an_annotation_compared_with_a_field_of_another_type_is_refused(db):
    await make_row()
    with pytest.raises(FieldError, match=r"filter\(size=...\) compares a number and text"):
        await ExpressionTypeRow.objects.annotate(size=Length("s")).filter(size=F("s")).count()
    with pytest.raises(FieldError, match=r"filter\(n=...\) compares a number and a boolean"):
        await ExpressionTypeRow.objects.values("flag").annotate(n=Count("id")).filter(n=F("flag")).count()


@pytest.mark.parametrize(
    ("aggregate", "message"),
    [
        (window.Sum("s"), r"Sum\(\) doesn't take text"),
        (window.Avg("flag"), r"Avg\(\) doesn't take a boolean"),
        (window.Max("flag"), r"Max\(\) doesn't take a boolean"),
    ],
)
@pytest.mark.asyncio
async def test_a_window_aggregate_of_values_it_doesnt_apply_to_is_refused(db, aggregate, message):
    await make_row()
    with pytest.raises(FieldError, match=message):
        await ExpressionTypeRow.objects.annotate(value=Window(aggregate)).values_list("value", flat=True)


@pytest.mark.asyncio
async def test_a_text_lookup_reads_a_boolean_as_true_or_false(db):
    await BooleanFields.objects.create(id=1, boolean=True)
    await BooleanFields.objects.create(id=2, boolean=False)
    assert await BooleanFields.objects.filter(boolean__icontains="t").values_list("id", flat=True) == [1]
    assert await BooleanFields.objects.filter(boolean__startswith="fal").values_list("id", flat=True) == [2]


@pytest.mark.asyncio
async def test_a_transformed_value_compares_with_a_value_of_its_own_type(db):
    row = await make_row()
    await ExpressionTypeRow.objects.filter(id=row.id).update(num=2020)
    assert await ExpressionTypeRow.objects.filter(dt__year=F("num")).count() == 1


@pytest.mark.asyncio
async def test_a_date_part_compared_with_text_is_refused(db):
    await make_row()
    with pytest.raises(FieldError, match=r"filter\(dt__year=...\) compares a number and text"):
        await ExpressionTypeRow.objects.filter(dt__year=F("s")).count()


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
