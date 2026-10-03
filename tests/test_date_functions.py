"""Extract*/Trunc*/Now and the chained date-part, __date and __time lookups, in a non-UTC zone."""

import datetime
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from hare.contrib.test.helpers import hare_test_context, truncate_all_models
from hare.exceptions import FieldError, UnSupportedError
from hare.query.functions import (
    Count,
    Extract,
    ExtractDay,
    ExtractIsoWeekDay,
    ExtractIsoYear,
    ExtractWeek,
    ExtractWeekDay,
    ExtractYear,
    Now,
    Trunc,
    TruncDate,
    TruncHour,
    TruncMinute,
    TruncMonth,
    TruncQuarter,
    TruncTime,
    TruncWeek,
)
from tests.date_function_models import Moment

MOSCOW = ZoneInfo("Europe/Moscow")


def get_test_db_url() -> str:
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url


@pytest_asyncio.fixture(scope="module")
async def date_functions_context() -> AsyncGenerator[Any]:
    async with hare_test_context(
        ["tests.date_function_models"], db_url=get_test_db_url(), timezone="Europe/Moscow", use_tz=True
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def moments(date_functions_context: Any) -> AsyncGenerator[tuple[Moment, Moment]]:
    # 2021-01-01 01:30:15 in Moscow - a Friday in ISO week 53 of 2020.
    new_year = await Moment.objects.create(
        id=1,
        at=datetime.datetime(2020, 12, 31, 22, 30, 15, 123456, tzinfo=datetime.UTC),
        day=datetime.date(2021, 1, 1),
        clock=datetime.time(1, 30, 15, 123456),
    )
    summer = await Moment.objects.create(
        id=2,
        at=datetime.datetime(2024, 6, 12, 9, 5, 0, tzinfo=datetime.UTC),
        day=datetime.date(2024, 6, 12),
        clock=datetime.time(12, 5),
    )
    yield new_year, summer
    await truncate_all_models()


async def get_first_value(expression: Any) -> Any:
    return (await Moment.objects.filter(id=1).annotate(value=expression).values_list("value", flat=True))[0]


@pytest.mark.asyncio
async def test_extract_parts_in_the_configured_zone(moments: tuple[Moment, Moment]) -> None:
    expected = {
        "year": 2021,
        "iso_year": 2020,
        "quarter": 1,
        "month": 1,
        "week": 53,
        "week_day": 6,
        "iso_week_day": 5,
        "day": 1,
        "hour": 1,
        "minute": 30,
        "second": 15,
        "microsecond": 123456,
    }
    for part, value in expected.items():
        assert await get_first_value(Extract("at", part)) == value, part
    assert await get_first_value(ExtractYear("day")) == 2021
    assert await get_first_value(ExtractIsoYear("day")) == 2020
    assert await get_first_value(ExtractWeek("day")) == 53
    assert await get_first_value(ExtractWeekDay("day")) == 6
    assert await get_first_value(ExtractIsoWeekDay("day")) == 5
    assert await get_first_value(Extract("clock", "minute")) == 30
    assert await Moment.objects.annotate(day_of_month=ExtractDay("at")).filter(day_of_month=12).count() == 1


@pytest.mark.asyncio
async def test_extract_rejects_a_part_the_value_has_none_of(moments: tuple[Moment, Moment]) -> None:
    with pytest.raises(FieldError, match="can't take 'hour' of a date"):
        await get_first_value(Extract("day", "hour"))
    with pytest.raises(FieldError, match="can't take 'year' of a time"):
        await get_first_value(Extract("clock", "year"))
    with pytest.raises(FieldError, match="needs a DateField, DatetimeField or TimeField"):
        await get_first_value(Extract("id", "year"))
    with pytest.raises(FieldError, match="part must be one of"):
        Extract("at", "century")


@pytest.mark.asyncio
async def test_trunc_types_in_the_configured_zone(moments: tuple[Moment, Moment]) -> None:
    assert await get_first_value(TruncMonth("at")) == datetime.datetime(2021, 1, 1, tzinfo=MOSCOW)
    assert await get_first_value(TruncQuarter("at")) == datetime.datetime(2021, 1, 1, tzinfo=MOSCOW)
    assert await get_first_value(TruncWeek("at")) == datetime.datetime(2020, 12, 28, tzinfo=MOSCOW)
    assert await get_first_value(TruncHour("at")) == datetime.datetime(2021, 1, 1, 1, tzinfo=MOSCOW)
    assert await get_first_value(TruncMinute("at")) == datetime.datetime(2021, 1, 1, 1, 30, tzinfo=MOSCOW)
    assert await get_first_value(TruncDate("at")) == datetime.date(2021, 1, 1)
    assert (await get_first_value(TruncTime("at"))).replace(tzinfo=None) == datetime.time(1, 30, 15, 123456)
    assert await get_first_value(TruncWeek("day")) == datetime.date(2020, 12, 28)
    assert await get_first_value(Trunc("day", "year")) == datetime.date(2021, 1, 1)
    assert (await get_first_value(TruncMinute("clock"))).replace(tzinfo=None) == datetime.time(1, 30)


@pytest.mark.asyncio
async def test_trunc_rejects_a_type_the_value_has_none_of(moments: tuple[Moment, Moment]) -> None:
    with pytest.raises(FieldError, match="can't truncate a date to 'hour'"):
        await get_first_value(TruncHour("day"))
    with pytest.raises(FieldError, match="can't truncate a time to 'month'"):
        await get_first_value(TruncMonth("clock"))
    with pytest.raises(FieldError, match="type must be one of"):
        Trunc("at", "decade")


@pytest.mark.asyncio
async def test_trunc_groups_rows(moments: tuple[Moment, Moment]) -> None:
    await Moment.objects.create(
        id=3,
        at=datetime.datetime(2024, 6, 30, 20, 59, tzinfo=datetime.UTC),
        day=datetime.date(2024, 6, 30),
        clock=datetime.time(23, 59),
    )
    rows = (
        await Moment.objects.annotate(month=TruncMonth("at"))
        .group_by("month")
        .annotate(count=Count("id"))
        .order_by("month")
        .values("month", "count")
    )
    assert rows == [
        {"month": datetime.datetime(2021, 1, 1, tzinfo=MOSCOW), "count": 1},
        {"month": datetime.datetime(2024, 6, 1, tzinfo=MOSCOW), "count": 2},
    ]


@pytest.mark.asyncio
async def test_now_in_a_filter_an_update_and_an_annotation(moments: tuple[Moment, Moment]) -> None:
    before = datetime.datetime.now(datetime.UTC)
    assert await Moment.objects.filter(at__lt=Now()).count() == 2
    await Moment.objects.filter(id=1).update(at=Now())
    updated = await Moment.objects.get(id=1)
    assert abs((updated.at - before).total_seconds()) < 60
    current = await get_first_value(Now())
    assert abs((current - before).total_seconds()) < 60


@pytest.mark.asyncio
async def test_date_part_lookups_take_chained_comparisons(moments: tuple[Moment, Moment]) -> None:
    async def get_ids(**filters: Any) -> list[int]:
        return sorted(await Moment.objects.filter(**filters).values_list("id", flat=True))

    assert await get_ids(at__year__gte=2021) == [1, 2]
    assert await get_ids(at__year__gt=2021) == [2]
    assert await get_ids(at__month__in=[1, 2]) == [1]
    assert await get_ids(at__week_day=6) == [1]
    assert await get_ids(at__iso_week_day=3) == [2]
    assert await get_ids(at__iso_year=2020) == [1]
    assert await get_ids(at__hour__not=1) == [2]
    assert await get_ids(day__week__gt=30) == [1]
    assert await get_ids(clock__hour__range=(0, 2)) == [1]
    # The same shape again with other bounds - the cached query must encode them anew.
    assert await get_ids(at__day__range=(1, 5)) == [1]
    assert await get_ids(at__day__range=(10, 20)) == [2]
    assert await get_ids(at__day__range=(2, 9)) == []


@pytest.mark.asyncio
async def test_date_and_time_lookups_read_the_datetime_in_the_configured_zone(moments: tuple[Moment, Moment]) -> None:
    async def get_ids(**filters: Any) -> list[int]:
        return sorted(await Moment.objects.filter(**filters).values_list("id", flat=True))

    assert await get_ids(at__date=datetime.date(2021, 1, 1)) == [1]
    assert await get_ids(at__date="2020-12-31") == []
    assert await get_ids(at__date__lt=datetime.date(2024, 1, 1)) == [1]
    assert await get_ids(at__date__range=(datetime.date(2021, 1, 1), datetime.date(2024, 6, 12))) == [1, 2]
    assert await get_ids(at__date__range=(datetime.date(2024, 6, 1), datetime.date(2024, 6, 30))) == [2]
    assert await get_ids(at__date__in=[datetime.date(2024, 6, 12)]) == [2]
    assert await get_ids(at__time=datetime.time(1, 30, 15, 123456)) == [1]
    assert await get_ids(at__time__gte="12:00") == [2]
    assert await get_ids(at__time__range=(datetime.time(0, 0), datetime.time(2, 0))) == [1]
    with pytest.raises(UnSupportedError, match="date__lt expects a date"):
        await get_ids(at__date__lt="not a date")
    with pytest.raises(UnSupportedError, match="year__in expects a list"):
        await get_ids(at__year__in=2021)
