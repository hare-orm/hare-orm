"""The current zone for a block of code (``Timezone.override()``) and an explicit zone of one
expression (``Trunc*``/``Extract*`` ``tzinfo=``): the date and time parts of a datetime are taken
in it - across midnight and a daylight saving change."""

from __future__ import annotations

import datetime

import pytest

from hare.exceptions import ConfigurationError
from hare.query.functions import Extract, TruncDay
from hare.utils import Timezone
from hare.utils.zone_info import ZoneInfo
from tests.testmodels import DatetimeFields

UTC = datetime.UTC
#: 22:30 UTC on March 28 - 01:30 on March 29 in Moscow (UTC+3).
LATE_EVENING = datetime.datetime(2026, 3, 28, 22, 30, tzinfo=UTC)
#: 00:30 and 01:30 UTC on March 29 - Berlin moves to summer time at 01:00 UTC: 01:30 CET, 03:30 CEST.
BEFORE_DST = datetime.datetime(2026, 3, 29, 0, 30, tzinfo=UTC)
AFTER_DST = datetime.datetime(2026, 3, 29, 1, 30, tzinfo=UTC)


async def create_rows() -> None:
    for row_id, moment in enumerate((LATE_EVENING, BEFORE_DST, AFTER_DST), start=1):
        await DatetimeFields.objects.create(id=row_id, datetime=moment)


@pytest.mark.asyncio
async def test_date_parts_follow_the_current_zone(db):
    await create_rows()
    march_28 = datetime.date(2026, 3, 28)
    march_29 = datetime.date(2026, 3, 29)
    with Timezone.override("UTC"):
        assert await DatetimeFields.objects.filter(datetime__date=march_28).count() == 1
        assert await DatetimeFields.objects.filter(id=1, datetime__hour=22).exists()
    with Timezone.override("Europe/Moscow"):
        assert Timezone.name() == "Europe/Moscow"
        assert await DatetimeFields.objects.filter(datetime__date=march_28).count() == 0
        assert await DatetimeFields.objects.filter(datetime__date=march_29).count() == 3
        assert await DatetimeFields.objects.filter(id=1, datetime__hour=1).exists()
    with Timezone.override(ZoneInfo("Europe/Berlin")):
        assert await DatetimeFields.objects.filter(datetime__hour=1).values_list("id", flat=True) == [2]
        assert await DatetimeFields.objects.filter(datetime__hour=3).values_list("id", flat=True) == [3]
    # The same query again in the UTC block - no cached SQL of another zone is reused.
    with Timezone.override("UTC"):
        assert await DatetimeFields.objects.filter(datetime__date=march_28).count() == 1


@pytest.mark.asyncio
async def test_an_expression_takes_its_own_zone(db):
    await create_rows()
    rows = (
        await DatetimeFields.objects.filter(id=1)
        .annotate(
            moscow_day=TruncDay("datetime", tzinfo="Europe/Moscow"),
            utc_day=TruncDay("datetime", tzinfo="UTC"),
            moscow_hour=Extract("datetime", "hour", tzinfo=ZoneInfo("Europe/Moscow")),
        )
        .values("moscow_day", "utc_day", "moscow_hour")
    )
    (row,) = rows
    assert row["moscow_day"] == datetime.datetime(2026, 3, 28, 21, 0, tzinfo=UTC)
    assert row["utc_day"] == datetime.datetime(2026, 3, 28, 0, 0, tzinfo=UTC)
    assert row["moscow_hour"] == 1
    berlin_hours = await (
        DatetimeFields.objects.filter(id__in=[2, 3])
        .order_by("id")
        .annotate(hour=Extract("datetime", "hour", tzinfo="Europe/Berlin"))
        .values_list("hour", flat=True)
    )
    assert list(berlin_hours) == [1, 3]


def test_an_unknown_zone_or_a_fixed_offset_is_refused():
    with pytest.raises(ConfigurationError), Timezone.override("Mars/Olympus_Mons"):
        pass
    with pytest.raises(ConfigurationError, match="IANA zone name"):
        TruncDay("datetime", tzinfo=datetime.timezone(datetime.timedelta(hours=3)))
