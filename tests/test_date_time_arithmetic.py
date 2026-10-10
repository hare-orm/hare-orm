"""Date/datetime/timedelta arithmetic in expressions - `datetime +/- timedelta`, `date +/-
timedelta`, `datetime - datetime`, `date - date`, `timedelta +/- timedelta` - must give the same
result on every backend, in annotations, filters, ordering, aggregates and `.update()`, under
`use_timezone=True` (any zone) and `use_timezone=False`. Every unsupported combination (a number against a
date/time, `datetime * 2`, `date + datetime`, ...) must raise a FieldError before anything is
written - SQLite used to silently store an INTEGER in a DATE column ("2024-01-31" + 1 == 2025) and
return garbage epoch datetimes for a difference, while Postgres raised a raw driver error."""

import datetime
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
import pytest_asyncio

from hare.exceptions import FieldError
from hare.query.expressions import ArithmeticOperator, CombinedExpression, F, OuterReference, Subquery, Value
from hare.query.functions import Avg, Count, Max, Min, Sum
from hare.time import Timezone
from tests import testmodels
from tests.utils.timezone_context import override_timezone

TemporalRecord = testmodels.TemporalRecord
TemporalBatch = testmodels.TemporalBatch

ZONES = [
    pytest.param((True, "Europe/Berlin"), id="tz-berlin"),
    pytest.param((True, "UTC"), id="tz-utc"),
    pytest.param((False, "UTC"), id="naive"),
]


@asynccontextmanager
async def temporal_zone(use_timezone: bool, timezone_name: str):
    """Runs the block under the given timezone mode, then removes the rows it created - rows written
    inside an overridden timezone context are not rolled back with the `db` fixture's transaction."""
    with override_timezone(use_timezone=use_timezone, timezone=timezone_name):
        try:
            yield
        finally:
            await TemporalRecord.objects.all().delete()
            await TemporalBatch.objects.all().delete()


@pytest_asyncio.fixture(params=ZONES)
async def zone(db, request):
    use_timezone, timezone_name = request.param
    async with temporal_zone(use_timezone, timezone_name):
        yield timezone_name


def moment(*args, **kwargs) -> datetime.datetime:
    """A datetime of the active timezone mode - aware UTC under use_timezone, naive otherwise."""
    if Timezone.get_use_timezone():
        return datetime.datetime(*args, tzinfo=datetime.UTC, **kwargs)
    return datetime.datetime(*args, **kwargs)


async def make_record(**overrides) -> testmodels.TemporalRecord:
    values = {"started_at": moment(2024, 1, 31, 12), "day": datetime.date(2024, 1, 31)}
    values.update(overrides)
    return await TemporalRecord.objects.create(**values)


async def evaluate(expression, **filters):
    """The value of `expression` for the one record matching `filters`."""
    return (
        await TemporalRecord.objects.filter(**filters).annotate(result=expression).values_list("result", flat=True)
    )[0]


# -- datetime +/- timedelta ---------------------------------------------------


@pytest.mark.asyncio
async def test_datetime_plus_timedelta_literal(zone):
    await make_record()
    assert await evaluate(F("started_at") + timedelta(hours=1)) == moment(2024, 1, 31, 13)
    assert await evaluate(F("started_at") + timedelta(days=1, minutes=30)) == moment(2024, 2, 1, 12, 30)


@pytest.mark.asyncio
async def test_datetime_minus_timedelta_literal_negative_and_microseconds(zone):
    await make_record(started_at=moment(2024, 3, 1, 0, 0, 0, 500000))
    assert await evaluate(F("started_at") - timedelta(microseconds=1)) == moment(2024, 3, 1, 0, 0, 0, 499999)
    assert await evaluate(F("started_at") - timedelta(seconds=-2)) == moment(2024, 3, 1, 0, 0, 2, 500000)
    assert await evaluate(F("started_at") + timedelta(microseconds=-500001)) == moment(2024, 2, 29, 23, 59, 59, 999999)
    assert await evaluate(F("started_at") - timedelta(days=61)) == moment(2023, 12, 31, 0, 0, 0, 500000)


@pytest.mark.asyncio
async def test_datetime_plus_timedelta_field_and_reflected_operand(zone):
    await make_record(duration=timedelta(hours=2, microseconds=7))
    assert await evaluate(F("started_at") + F("duration")) == moment(2024, 1, 31, 14, 0, 0, 7)
    assert await evaluate(F("started_at") - F("duration")) == moment(2024, 1, 31, 9, 59, 59, 999993)
    assert await evaluate(timedelta(hours=1) + F("started_at")) == moment(2024, 1, 31, 13)


@pytest.mark.asyncio
async def test_datetime_literal_as_the_left_operand_of_a_shift(zone):
    await make_record(duration=timedelta(minutes=10))
    assert await evaluate(Value(moment(2024, 5, 1, 8)) + F("duration")) == moment(2024, 5, 1, 8, 10)


@pytest.mark.asyncio
async def test_datetime_shift_result_is_an_aware_datetime_in_the_configured_zone(zone):
    await make_record()
    result = await evaluate(F("started_at") + timedelta(hours=1))
    if Timezone.get_use_timezone():
        assert result.utcoffset() is not None
        assert result.tzinfo == Timezone.default()
    else:
        assert result.tzinfo is None


@pytest.mark.asyncio
async def test_datetime_shift_is_absolute_time_across_a_dst_transition(db):
    """24 hours after noon (Berlin, CET) on 2024-03-30 is 13:00 on the 31st - the clocks moved
    forward at 02:00 in between - on every backend."""
    async with temporal_zone(True, "Europe/Berlin"):
        berlin = Timezone.default()
        await make_record(started_at=datetime.datetime(2024, 3, 30, 12, tzinfo=berlin))
        assert await evaluate(F("started_at") + timedelta(days=1)) == datetime.datetime(2024, 3, 31, 13, tzinfo=berlin)
        assert await evaluate(F("started_at") - timedelta(days=1)) == datetime.datetime(2024, 3, 29, 12, tzinfo=berlin)


@pytest.mark.asyncio
async def test_datetime_shift_result_survives_a_write_read_round_trip(zone):
    """The shifted value is stored in the same format a plain write produces, so ordering and
    comparisons against ordinarily-written rows stay correct."""
    record = await make_record()
    await TemporalRecord.objects.filter(id=record.id).update(started_at=F("started_at") + timedelta(hours=5))
    await make_record(started_at=moment(2024, 1, 31, 16, 59, 59))
    await make_record(started_at=moment(2024, 1, 31, 17, 0, 1))
    ordered = await TemporalRecord.objects.all().order_by("started_at").values_list("started_at", flat=True)
    assert ordered == [moment(2024, 1, 31, 16, 59, 59), moment(2024, 1, 31, 17), moment(2024, 1, 31, 17, 0, 1)]


# -- datetime - datetime, date - date -----------------------------------------


@pytest.mark.asyncio
async def test_datetime_difference_is_a_timedelta_with_microsecond_precision(zone):
    await make_record(started_at=moment(2024, 1, 31, 12, 0, 0, 123456), finished_at=moment(2026, 9, 21, 15, 0, 0, 7))
    difference = await evaluate(F("finished_at") - F("started_at"))
    assert difference == moment(2026, 9, 21, 15, 0, 0, 7) - moment(2024, 1, 31, 12, 0, 0, 123456)
    assert difference == timedelta(days=964, hours=3, microseconds=-123449)
    assert await evaluate(F("started_at") - F("finished_at")) == -difference


@pytest.mark.asyncio
async def test_datetime_difference_against_a_literal_datetime(zone):
    await make_record(started_at=moment(2024, 1, 31, 12))
    assert await evaluate(F("started_at") - moment(2024, 1, 31, 10, 30)) == timedelta(hours=1, minutes=30)
    assert await evaluate(moment(2024, 1, 31, 14) - F("started_at")) == timedelta(hours=2)


@pytest.mark.asyncio
async def test_datetime_difference_across_a_dst_transition_is_absolute_time(db):
    """Noon to noon across the 2024-03-31 spring-forward is 23 hours, not 24."""
    async with temporal_zone(True, "Europe/Berlin"):
        berlin = Timezone.default()
        await make_record(
            started_at=datetime.datetime(2024, 3, 30, 12, tzinfo=berlin),
            finished_at=datetime.datetime(2024, 3, 31, 12, tzinfo=berlin),
        )
        assert await evaluate(F("finished_at") - F("started_at")) == timedelta(hours=23)


@pytest.mark.asyncio
async def test_date_difference_is_a_whole_number_of_days(zone):
    await make_record(day=datetime.date(2024, 1, 31), other_day=datetime.date(2024, 3, 1))
    assert await evaluate(F("other_day") - F("day")) == timedelta(days=30)
    assert await evaluate(F("day") - F("other_day")) == timedelta(days=-30)
    assert await evaluate(F("other_day") - datetime.date(2023, 3, 1)) == timedelta(days=366)


# -- date +/- timedelta -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "delta", "expected"),
    [
        (datetime.date(2024, 1, 31), timedelta(days=1), datetime.date(2024, 2, 1)),
        (datetime.date(2024, 1, 31), timedelta(days=29), datetime.date(2024, 2, 29)),
        (datetime.date(2024, 2, 28), timedelta(days=1), datetime.date(2024, 2, 29)),
        (datetime.date(2023, 2, 28), timedelta(days=1), datetime.date(2023, 3, 1)),
        (datetime.date(2024, 12, 31), timedelta(days=1), datetime.date(2025, 1, 1)),
        (datetime.date(2024, 3, 1), timedelta(days=-1), datetime.date(2024, 2, 29)),
        (datetime.date(2024, 1, 31), timedelta(days=366), datetime.date(2025, 1, 31)),
    ],
)
async def test_date_plus_timedelta_calendar_boundaries(zone, start, delta, expected):
    await make_record(day=start)
    assert await evaluate(F("day") + delta) == expected
    assert await evaluate(F("day") - delta) == start - delta


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delta", "expected_plus", "expected_minus"),
    [
        (timedelta(hours=1), datetime.date(2024, 6, 15), datetime.date(2024, 6, 14)),
        (timedelta(hours=23, minutes=59), datetime.date(2024, 6, 15), datetime.date(2024, 6, 14)),
        (timedelta(hours=36), datetime.date(2024, 6, 16), datetime.date(2024, 6, 13)),
        (timedelta(hours=-1), datetime.date(2024, 6, 14), datetime.date(2024, 6, 15)),
        (timedelta(microseconds=1), datetime.date(2024, 6, 15), datetime.date(2024, 6, 14)),
    ],
)
async def test_date_shift_floors_a_sub_day_remainder(zone, delta, expected_plus, expected_minus):
    """A date is midnight: adding a sub-day amount stays on the date (floor), subtracting one
    reaches the previous date - the result is the date of `midnight +/- delta`."""
    await make_record(day=datetime.date(2024, 6, 15))
    assert await evaluate(F("day") + delta) == expected_plus
    assert await evaluate(F("day") - delta) == expected_minus


@pytest.mark.asyncio
async def test_date_plus_timedelta_field(zone):
    await make_record(day=datetime.date(2024, 1, 31), duration=timedelta(days=2, hours=5))
    assert await evaluate(F("day") + F("duration")) == datetime.date(2024, 2, 2)
    # midnight of the 31st minus 53 hours is the evening of the 28th
    assert await evaluate(F("day") - F("duration")) == datetime.date(2024, 1, 28)


# -- timedelta +/- timedelta --------------------------------------------------


@pytest.mark.asyncio
async def test_timedelta_plus_minus_timedelta(zone):
    await make_record(duration=timedelta(seconds=5, microseconds=3), other_duration=timedelta(seconds=7))
    assert await evaluate(F("duration") + F("other_duration")) == timedelta(seconds=12, microseconds=3)
    assert await evaluate(F("duration") - F("other_duration")) == timedelta(seconds=-2, microseconds=3)
    assert await evaluate(F("duration") + timedelta(hours=1)) == timedelta(hours=1, seconds=5, microseconds=3)
    assert await evaluate(F("duration") - timedelta(seconds=10)) == timedelta(seconds=-5, microseconds=3)
    assert await evaluate(timedelta(days=1) - F("other_duration")) == timedelta(days=1, seconds=-7)


@pytest.mark.asyncio
async def test_timedelta_multiplied_by_a_number_is_unchanged(zone):
    """Scaling a duration by a plain number stays ordinary microsecond arithmetic."""
    await make_record(duration=timedelta(seconds=5))
    assert await evaluate(F("duration") * 2) == 10_000_000


# -- NULL ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_operands_give_null_without_raising(zone):
    await make_record(finished_at=None, duration=None, other_day=None)
    assert await evaluate(F("duration") + timedelta(hours=1)) is None
    assert await evaluate(F("finished_at") + timedelta(hours=1)) is None
    assert await evaluate(F("finished_at") - timedelta(hours=1)) is None
    assert await evaluate(F("started_at") + F("duration")) is None
    assert await evaluate(F("finished_at") - F("started_at")) is None
    assert await evaluate(F("started_at") - F("finished_at")) is None
    assert await evaluate(F("other_day") + timedelta(days=1)) is None
    assert await evaluate(F("day") - F("other_day")) is None
    assert await evaluate(F("duration") + F("duration")) is None


# -- update -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_datetime_from_a_shifted_expression(zone):
    record = await make_record(finished_at=moment(2024, 1, 31, 8))
    await TemporalRecord.objects.filter(id=record.id).update(
        started_at=F("started_at") + timedelta(hours=1, microseconds=5),
        finished_at=F("finished_at") - timedelta(days=1),
    )
    await record.refresh_from_db()
    assert record.started_at == moment(2024, 1, 31, 13, 0, 0, 5)
    assert record.finished_at == moment(2024, 1, 30, 8)


@pytest.mark.asyncio
async def test_update_date_and_timedelta_from_shifted_expressions(zone):
    record = await make_record(day=datetime.date(2024, 1, 31), duration=timedelta(seconds=5))
    await TemporalRecord.objects.filter(id=record.id).update(
        day=F("day") + timedelta(days=1), duration=F("duration") + timedelta(seconds=1)
    )
    await record.refresh_from_db()
    assert record.day == datetime.date(2024, 2, 1)
    assert record.duration == timedelta(seconds=6)


@pytest.mark.asyncio
async def test_update_datetime_field_from_a_datetime_difference_is_a_timedelta_field_update(zone):
    record = await make_record(started_at=moment(2024, 1, 31, 12), finished_at=moment(2024, 1, 31, 14, 30))
    await TemporalRecord.objects.filter(id=record.id).update(duration=F("finished_at") - F("started_at"))
    await record.refresh_from_db()
    assert record.duration == timedelta(hours=2, minutes=30)


@pytest.mark.asyncio
async def test_update_with_a_null_operand_writes_null(zone):
    record = await make_record(duration=None, finished_at=None)
    await TemporalRecord.objects.filter(id=record.id).update(
        other_duration=F("duration") + timedelta(seconds=1), other_day=F("day") + timedelta(days=1)
    )
    await record.refresh_from_db()
    assert record.other_duration is None
    assert record.other_day == datetime.date(2024, 2, 1)


# -- filter / order_by --------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_by_a_shifted_expression(zone):
    early = await make_record(started_at=moment(2024, 1, 31, 12), finished_at=moment(2024, 1, 31, 11))
    late = await make_record(started_at=moment(2024, 1, 31, 12), finished_at=moment(2024, 1, 31, 9))
    result = await TemporalRecord.objects.filter(started_at__gt=F("finished_at") + timedelta(hours=2)).values_list(
        "id", flat=True
    )
    assert result == [late.id]
    result = await TemporalRecord.objects.filter(started_at__lte=F("finished_at") + timedelta(hours=2)).values_list(
        "id", flat=True
    )
    assert result == [early.id]


@pytest.mark.asyncio
async def test_filter_by_an_annotated_difference(zone):
    short = await make_record(started_at=moment(2024, 1, 31, 12), finished_at=moment(2024, 1, 31, 12, 30))
    await make_record(started_at=moment(2024, 1, 31, 12), finished_at=moment(2024, 1, 31, 15))
    result = (
        await TemporalRecord.objects.annotate(span=F("finished_at") - F("started_at"))
        .filter(span__lt=timedelta(hours=1))
        .values_list("id", flat=True)
    )
    assert result == [short.id]


@pytest.mark.asyncio
async def test_order_by_a_shifted_expression(zone):
    first = await make_record(started_at=moment(2024, 1, 31, 12), duration=timedelta(hours=5))
    second = await make_record(started_at=moment(2024, 1, 31, 14), duration=timedelta(hours=1))
    third = await make_record(started_at=moment(2024, 1, 31, 13), duration=timedelta(hours=3))
    ends = TemporalRecord.objects.annotate(ends_at=F("started_at") + F("duration"))
    assert await ends.order_by("ends_at").values_list("id", flat=True) == [second.id, third.id, first.id]
    assert await ends.order_by("-ends_at").values_list("id", flat=True) == [first.id, third.id, second.id]


# -- aggregates ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregates_over_a_difference(zone):
    await make_record(started_at=moment(2024, 1, 31, 12), finished_at=moment(2024, 1, 31, 13))
    await make_record(started_at=moment(2024, 1, 31, 12), finished_at=moment(2024, 1, 31, 15))
    await make_record(started_at=moment(2024, 1, 31, 12), finished_at=None)
    span = F("finished_at") - F("started_at")
    result = await TemporalRecord.objects.all().aggregate(
        longest=Max(span), shortest=Min(span), mean=Avg(span), total=Sum(span)
    )
    assert result == {
        "longest": timedelta(hours=3),
        "shortest": timedelta(hours=1),
        "mean": timedelta(hours=2),
        "total": timedelta(hours=4),
    }


@pytest.mark.asyncio
async def test_aggregate_of_a_date_difference(zone):
    await make_record(day=datetime.date(2024, 1, 1), other_day=datetime.date(2024, 1, 11))
    await make_record(day=datetime.date(2024, 1, 1), other_day=datetime.date(2024, 1, 4))
    result = await TemporalRecord.objects.all().aggregate(longest=Max(F("other_day") - F("day")))
    assert result == {"longest": timedelta(days=10)}


@pytest.mark.asyncio
async def test_sum_and_max_over_a_shifted_timedelta(zone):
    await make_record(duration=timedelta(seconds=5))
    await make_record(duration=timedelta(seconds=10))
    await make_record(duration=None)
    result = await TemporalRecord.objects.all().aggregate(
        total=Sum(F("duration") + timedelta(seconds=1)), longest=Max(F("duration") + timedelta(hours=1))
    )
    assert result == {"total": timedelta(seconds=17), "longest": timedelta(hours=1, seconds=10)}


@pytest.mark.asyncio
async def test_max_over_a_shifted_datetime_and_date(zone):
    await make_record(started_at=moment(2024, 1, 31, 12), day=datetime.date(2024, 1, 31))
    await make_record(started_at=moment(2024, 2, 1, 12), day=datetime.date(2024, 2, 1))
    result = await TemporalRecord.objects.all().aggregate(
        latest=Max(F("started_at") + timedelta(hours=1)), latest_day=Max(F("day") + timedelta(days=1))
    )
    assert result == {"latest": moment(2024, 2, 1, 13), "latest_day": datetime.date(2024, 2, 2)}


@pytest.mark.asyncio
async def test_nested_arithmetic(zone):
    await make_record(started_at=moment(2024, 1, 31, 12), duration=timedelta(hours=1))
    shifted = F("started_at") + timedelta(hours=2)
    assert await evaluate(shifted - F("started_at")) == timedelta(hours=2)
    assert await evaluate(
        CombinedExpression(F("started_at") + F("duration"), ArithmeticOperator.ADD, timedelta(hours=1))
    ) == moment(2024, 1, 31, 14)
    assert await evaluate(F("duration") + (shifted - F("started_at"))) == timedelta(hours=3)


# -- correlated subquery ------------------------------------------------------


@pytest.mark.asyncio
async def test_subquery_over_a_shifted_datetime(zone):
    batch = await TemporalBatch.objects.create(name="only")
    await make_record(batch=batch, started_at=moment(2024, 1, 31, 12))
    await make_record(batch=batch, started_at=moment(2024, 2, 1, 12))
    latest = (
        TemporalRecord.objects.filter(batch_id=OuterReference("id"))
        .annotate(latest=Max(F("started_at") + timedelta(hours=1)))
        .group_by("batch_id")
        .values("latest")
    )
    result = await TemporalBatch.objects.all().annotate(value=Subquery(latest)).values_list("value", flat=True)
    assert result == [moment(2024, 2, 1, 13)]


# -- unsupported combinations -------------------------------------------------

UNSUPPORTED = {
    "date + number": lambda: F("day") + 1,
    "date - number": lambda: F("day") - 1,
    "number + date": lambda: 1 + F("day"),
    "datetime + number": lambda: F("started_at") + 1,
    "datetime * 2": lambda: F("started_at") * 2,
    "datetime / 2": lambda: F("started_at") / 2,
    "datetime + datetime": lambda: F("started_at") + F("finished_at"),
    "date + datetime": lambda: F("day") + F("started_at"),
    "datetime - date": lambda: F("started_at") - F("day"),
    "date - datetime": lambda: F("day") - F("started_at"),
    "timedelta - datetime": lambda: F("duration") - F("started_at"),
    "timedelta - date": lambda: F("duration") - F("day"),
    "date + date": lambda: F("day") + F("other_day"),
    "date * timedelta": lambda: F("day") * timedelta(days=1),
    "datetime * timedelta": lambda: F("started_at") * F("duration"),
    "timedelta * timedelta": lambda: F("duration") * F("other_duration"),
    "time + timedelta": lambda: F("at_time") + timedelta(hours=1),
    "time + time": lambda: F("at_time") + F("at_time"),
    "time - number": lambda: F("at_time") - 1,
    "time literal + timedelta": lambda: Value(datetime.time(1, 2)) + F("duration"),
    "int + timedelta literal": lambda: F("quantity") + timedelta(seconds=1),
    "timedelta literal + number literal": lambda: CombinedExpression(
        Value(timedelta(seconds=1)), ArithmeticOperator.ADD, 5
    ),
    "date + string": lambda: F("day") + "x",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("build", list(UNSUPPORTED.values()), ids=list(UNSUPPORTED))
async def test_unsupported_combination_raises_field_error(zone, build):
    await make_record()
    with pytest.raises(FieldError, match="Unsupported arithmetic"):
        await TemporalRecord.objects.annotate(result=build()).values_list("result", flat=True)


@pytest.mark.asyncio
async def test_unsupported_update_raises_and_writes_nothing(zone):
    record = await make_record(day=datetime.date(2024, 1, 31))
    with pytest.raises(FieldError, match="Unsupported arithmetic"):
        await TemporalRecord.objects.filter(id=record.id).update(day=F("day") + 1)
    with pytest.raises(FieldError, match="Unsupported arithmetic"):
        await TemporalRecord.objects.filter(id=record.id).update(started_at=F("started_at") * 2)
    reread = await TemporalRecord.objects.get(id=record.id)
    assert reread.day == datetime.date(2024, 1, 31)
    assert reread.started_at == moment(2024, 1, 31, 12)


@pytest.mark.asyncio
async def test_update_from_an_expression_of_another_date_time_type_raises_and_writes_nothing(zone):
    """A datetime shift can't be stored in a date column, nor a difference in a datetime one."""
    record = await make_record(day=datetime.date(2024, 1, 31), finished_at=moment(2024, 2, 1))
    with pytest.raises(FieldError, match="Cannot update 'day'"):
        await TemporalRecord.objects.filter(id=record.id).update(day=F("started_at") + timedelta(days=1))
    with pytest.raises(FieldError, match="Cannot update 'started_at'"):
        await TemporalRecord.objects.filter(id=record.id).update(started_at=F("finished_at") - F("started_at"))
    reread = await TemporalRecord.objects.get(id=record.id)
    assert reread.day == datetime.date(2024, 1, 31)
    assert reread.started_at == moment(2024, 1, 31, 12)


@pytest.mark.asyncio
async def test_unsupported_annotation_in_an_aggregate_raises(zone):
    await make_record()
    with pytest.raises(FieldError, match="Unsupported arithmetic"):
        await TemporalRecord.objects.all().aggregate(total=Sum(F("day") + 1))


@pytest.mark.asyncio
async def test_count_of_a_date_field_is_still_plain_arithmetic(zone):
    """Count("day") is an integer, whatever its argument's field type - the date rules don't
    apply to it."""
    await make_record()
    assert await evaluate(CombinedExpression(Count("day"), ArithmeticOperator.ADD, 1)) == 2


# -- QUERY_SHAPE_CACHE --------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_query_rebinds_a_timedelta_literal(zone):
    await make_record(started_at=moment(2024, 1, 31, 12), duration=timedelta(seconds=5))
    for hours in (1, 2, 5, 1, 24):
        assert await evaluate(F("started_at") + timedelta(hours=hours)) == moment(2024, 1, 31, 12) + timedelta(
            hours=hours
        )
        assert await evaluate(F("duration") + timedelta(hours=hours)) == timedelta(hours=hours, seconds=5)
        assert await evaluate(F("day") + timedelta(days=hours)) == datetime.date(2024, 1, 31) + timedelta(days=hours)


@pytest.mark.asyncio
async def test_repeated_query_rebinds_a_datetime_and_date_literal(zone):
    await make_record(started_at=moment(2024, 1, 31, 12), day=datetime.date(2024, 1, 31))
    for hour in (10, 9, 8, 10):
        assert await evaluate(F("started_at") - moment(2024, 1, 31, hour)) == timedelta(hours=12 - hour)
    for day in (30, 29, 30):
        assert await evaluate(F("day") - datetime.date(2024, 1, day)) == timedelta(days=31 - day)


@pytest.mark.asyncio
async def test_same_annotation_shape_with_a_different_literal_type_is_not_reused(zone):
    """`F("quantity") + 1` is cached first - the same-shaped `F("quantity") + timedelta(...)` must
    not reuse that structure (and its numeric binding)."""
    await make_record(quantity=3)
    assert await evaluate(F("quantity") + 1) == 4
    with pytest.raises(FieldError, match="Unsupported arithmetic"):
        await evaluate(F("quantity") + timedelta(seconds=1))
    assert await evaluate(F("quantity") + 2) == 5
