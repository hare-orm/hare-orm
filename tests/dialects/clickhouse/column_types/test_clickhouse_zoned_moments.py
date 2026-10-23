"""Moments of a ClickHouse table under a configured zone that isn't UTC, on both drivers: read as the
instant they are (clickhouse-connect returns them naive, their wall clock UTC), truncated at the
zone - a week, a month or a year too - ``Now()`` in a filter, an update and a column default, and
the parts of a time of day read from its wall clock."""

import datetime
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.query.expressions import Value
from hare.query.functions import (
    Cast,
    Concat,
    Extract,
    Now,
    Trunc,
    TruncHour,
    TruncMinute,
    TruncMonth,
    TruncWeek,
    TruncYear,
)
from tests.dialects.clickhouse.column_types.zoned_moment_models import ZonedMoment

#: The models of the tests opening a context of their own.
ZONED_MOMENT_MODELS_MODULE = "tests.dialects.clickhouse.column_types.zoned_moment_models"

MOSCOW = ZoneInfo("Europe/Moscow")


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest_asyncio.fixture
async def moments() -> AsyncGenerator[Any]:
    raw_db_url = os.getenv("HARE_TEST_DB", "").replace("\\{", "{").replace("\\}", "}")
    db_url = raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url
    async with hare_test_context(
        [ZONED_MOMENT_MODELS_MODULE],
        db_url=db_url,
        timezone="Europe/Moscow",
        use_timezone=True,
    ) as context:
        # 2021-01-01 01:30:15 in Moscow.
        await ZonedMoment.objects.create(
            id=1, at=datetime.datetime(2020, 12, 31, 22, 30, 15, tzinfo=datetime.UTC), clock=datetime.time(1, 30, 15)
        )
        yield context


async def get_value(expression: Any) -> Any:
    return (await ZonedMoment.objects.filter(id=1).annotate(value=expression).values_list("value", flat=True))[0]


@pytest.mark.asyncio
async def test_a_moment_reads_back_as_the_instant_it_is(moments):
    moment = datetime.datetime(2021, 1, 1, 1, 30, 15, tzinfo=MOSCOW)
    assert (await ZonedMoment.objects.get(id=1)).at == moment
    assert await ZonedMoment.objects.filter(id=1).values_list("at", flat=True) == [moment]
    assert await ZonedMoment.objects.filter(at=moment).count() == 1


@pytest.mark.asyncio
async def test_a_moment_is_truncated_at_the_zone(moments):
    assert await get_value(TruncYear("at")) == datetime.datetime(2021, 1, 1, tzinfo=MOSCOW)
    assert await get_value(TruncMonth("at")) == datetime.datetime(2021, 1, 1, tzinfo=MOSCOW)
    assert await get_value(TruncWeek("at")) == datetime.datetime(2020, 12, 28, tzinfo=MOSCOW)
    assert await get_value(TruncHour("at")) == datetime.datetime(2021, 1, 1, 1, tzinfo=MOSCOW)
    assert (await get_value(TruncMinute("clock"))).replace(tzinfo=None) == datetime.time(1, 30)


@pytest.mark.asyncio
async def test_a_moment_and_a_date_before_1970_are_truncated_down(moments):
    """ClickHouse's dateTrunc() rounded a moment before 1970 with a fraction toward 1970 (23:59:59.5
    of 1969-12-31 truncated to 1970-01-01), and on 24.3 wrapped any moment or date before 1970 past
    2106."""
    await ZonedMoment.objects.bulk_create(
        [
            ZonedMoment(
                id=2,
                at=datetime.datetime(1960, 6, 15, 15, 34, 56, 123456, tzinfo=MOSCOW),
                clock=datetime.time(0),
                day=datetime.date(1960, 6, 15),
            ),
            ZonedMoment(
                id=3, at=datetime.datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=MOSCOW), clock=datetime.time(0)
            ),
        ]
    )

    async def get_truncated(row_id: int, unit: str, field_name: str = "at") -> Any:
        rows = ZonedMoment.objects.filter(id=row_id).annotate(value=Trunc(field_name, unit))
        return (await rows.values_list("value", flat=True))[0]

    expected = {
        "second": datetime.datetime(1960, 6, 15, 15, 34, 56, tzinfo=MOSCOW),
        "minute": datetime.datetime(1960, 6, 15, 15, 34, tzinfo=MOSCOW),
        "hour": datetime.datetime(1960, 6, 15, 15, tzinfo=MOSCOW),
        "day": datetime.datetime(1960, 6, 15, tzinfo=MOSCOW),
        "week": datetime.datetime(1960, 6, 13, tzinfo=MOSCOW),
        "month": datetime.datetime(1960, 6, 1, tzinfo=MOSCOW),
        "quarter": datetime.datetime(1960, 4, 1, tzinfo=MOSCOW),
        "year": datetime.datetime(1960, 1, 1, tzinfo=MOSCOW),
    }
    assert {unit: await get_truncated(2, unit) for unit in expected} == expected
    assert await get_truncated(3, "second") == datetime.datetime(1969, 12, 31, 23, 59, 59, tzinfo=MOSCOW)
    assert await get_truncated(3, "day") == datetime.datetime(1969, 12, 31, tzinfo=MOSCOW)
    assert await get_truncated(3, "year") == datetime.datetime(1969, 1, 1, tzinfo=MOSCOW)
    assert {unit: await get_truncated(2, unit, "day") for unit in ("week", "month", "quarter", "year", "day")} == {
        "week": datetime.date(1960, 6, 13),
        "month": datetime.date(1960, 6, 1),
        "quarter": datetime.date(1960, 4, 1),
        "year": datetime.date(1960, 1, 1),
        "day": datetime.date(1960, 6, 15),
    }


@pytest.mark.asyncio
async def test_now_filters_updates_and_defaults_a_column(moments):
    before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    stamped = (await ZonedMoment.objects.get(id=1)).stamped
    assert before < stamped <= datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5)
    assert await ZonedMoment.objects.filter(at__lt=Now()).count() == 1
    await ZonedMoment.objects.filter(id=1).update(at=Now())
    assert (await ZonedMoment.objects.get(id=1)).at > before


@pytest.mark.asyncio
async def test_a_time_of_day_is_read_from_its_wall_clock(moments):
    assert await get_value(Extract("clock", "hour")) == 1
    assert await get_value(Extract("clock", "minute")) == 30
    assert await ZonedMoment.objects.filter(clock__hour__range=(0, 2)).count() == 1
    assert await ZonedMoment.objects.filter(clock__hour=22).count() == 0


@pytest.mark.asyncio
async def test_a_naive_moment_without_time_zones_is_its_local_wall_clock():
    raw_db_url = os.getenv("HARE_TEST_DB", "").replace("\\{", "{").replace("\\}", "}")
    db_url = raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url
    async with hare_test_context([ZONED_MOMENT_MODELS_MODULE], db_url=db_url, use_timezone=False):
        wall_clock = datetime.datetime(2020, 6, 1, 12, 30)
        await ZonedMoment.objects.create(id=1, at=wall_clock, clock=datetime.time(12, 30))
        await ZonedMoment.objects.bulk_create([ZonedMoment(id=2, at=wall_clock, clock=datetime.time(12, 30))])
        assert await ZonedMoment.objects.order_by("id").values_list("at", flat=True) == [wall_clock, wall_clock]
        assert (await ZonedMoment.objects.get(id=1)).at == wall_clock
        assert await ZonedMoment.objects.filter(at=wall_clock, at__hour=12, at__date=wall_clock.date()).count() == 2
        assert await ZonedMoment.objects.annotate(hour=Extract("at", "hour")).values_list("hour", flat=True) == [
            12,
            12,
        ]


@pytest.mark.asyncio
async def test_a_naive_moment_is_cast_and_written_as_text_at_its_local_wall_clock():
    """Without time zones the column holds the instant of the local wall clock - the casts to text, a
    date and a time of day, Concat and a date cast to a moment read it in UTC, a machine's offset away
    from what the model reads."""
    raw_db_url = os.getenv("HARE_TEST_DB", "").replace("\\{", "{").replace("\\}", "}")
    db_url = raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url
    async with hare_test_context([ZONED_MOMENT_MODELS_MODULE], db_url=db_url, use_timezone=False):
        wall_clock = datetime.datetime(2024, 3, 1, 1, 30)
        await ZonedMoment.objects.create(
            id=1, at=wall_clock, clock=datetime.time(1, 30), day=datetime.date(2024, 3, 1)
        )
        assert await get_value(Cast("at", fields.TextField())) == "2024-03-01 01:30:00.000000"
        assert await get_value(Concat("at", Value("|"))) == "2024-03-01 01:30:00.000000|"
        assert await get_value(Concat(Value(wall_clock), Value("|"))) == "2024-03-01 01:30:00|"
        assert await get_value(Concat(Value(wall_clock.astimezone()), Value("|"))) == "2024-03-01 01:30:00|"
        assert await get_value(Cast("at", fields.DateField())) == datetime.date(2024, 3, 1)
        assert await get_value(Cast("at", fields.TimeField())) == datetime.time(1, 30)
        assert await get_value(Cast("day", fields.DatetimeField())) == datetime.datetime(2024, 3, 1)
        assert await get_value(Cast(Value("2024-03-01 01:30:00"), fields.DatetimeField())) == wall_clock
