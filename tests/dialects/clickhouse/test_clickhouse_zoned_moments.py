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

from hare.contrib.test.isolated_contexts import hare_test_context
from hare.query.functions import Extract, Now, TruncHour, TruncMinute, TruncMonth, TruncWeek, TruncYear
from tests.dialects.clickhouse.zoned_moment_models import ZonedMoment

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
        ["tests.dialects.clickhouse.zoned_moment_models"], db_url=db_url, timezone="Europe/Moscow", use_timezone=True
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
async def test_now_filters_updates_and_defaults_a_column(moments):
    before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    stamped = (await ZonedMoment.objects.get(id=1)).stamped
    assert before < stamped <= datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5)
    assert await ZonedMoment.objects.filter(at__lt=Now()).count() == 1
    assert await ZonedMoment.objects.filter(id=1).update(at=Now()) == 1
    assert (await ZonedMoment.objects.get(id=1)).at > before


@pytest.mark.asyncio
async def test_a_time_of_day_is_read_from_its_wall_clock(moments):
    assert await get_value(Extract("clock", "hour")) == 1
    assert await get_value(Extract("clock", "minute")) == 30
    assert await ZonedMoment.objects.filter(clock__hour__range=(0, 2)).count() == 1
    assert await ZonedMoment.objects.filter(clock__hour=22).count() == 0


@pytest.mark.asyncio
async def test_a_naive_moment_without_time_zones_is_its_local_wall_clock():
    raw_db_url = os.getenv("HARE_TEST_DB", "").replace("\{", "{").replace("\}", "}")
    db_url = raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url
    async with hare_test_context(["tests.dialects.clickhouse.zoned_moment_models"], db_url=db_url, use_timezone=False):
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
