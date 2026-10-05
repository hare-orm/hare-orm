"""A part of a date, time or datetime field read by name, like the ``__year``/``__date`` lookups:
``values("created__year")``, ``values_list("starts__hour")``, ``order_by("-created__month")``,
``distinct("created__date")`` - through relations too, in the current zone."""

from __future__ import annotations

import datetime

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import FieldError
from hare.query.functions import Count, ExtractYear
from hare.time import Timezone
from tests.testmodels import DateFields, DatetimeFields, Event, TimeFields, Tournament

UTC = datetime.UTC
#: 22:30 UTC on March 28 - 01:30 on March 29 in Moscow (UTC+3).
LATE_EVENING = datetime.datetime(2026, 3, 28, 22, 30, 15, 250, tzinfo=UTC)
MORNING = datetime.datetime(2025, 11, 3, 9, 5, tzinfo=UTC)


async def create_datetimes() -> None:
    await DatetimeFields.objects.create(id=1, datetime=LATE_EVENING)
    await DatetimeFields.objects.create(id=2, datetime=MORNING)


@pytest.mark.asyncio
async def test_values_read_datetime_parts(db):
    await create_datetimes()
    with Timezone.override("UTC"):
        rows = (
            await DatetimeFields.objects.all()
            .order_by("id")
            .values(
                "id",
                "datetime__year",
                "datetime__quarter",
                "datetime__month",
                "datetime__day",
                "datetime__week_day",
                "datetime__iso_week_day",
                "datetime__hour",
                "datetime__minute",
                "datetime__second",
                "datetime__date",
                "datetime__time",
            )
        )
    # A time of day under use_timezone carries the zone's offset, as a TimeField's value does.
    assert rows[0].pop("datetime__time").replace(tzinfo=None) == datetime.time(22, 30, 15, 250)
    assert rows[0] == {
        "id": 1,
        "datetime__year": 2026,
        "datetime__quarter": 1,
        "datetime__month": 3,
        "datetime__day": 28,
        "datetime__week_day": 7,
        "datetime__iso_week_day": 6,
        "datetime__hour": 22,
        "datetime__minute": 30,
        "datetime__second": 15,
        "datetime__date": datetime.date(2026, 3, 28),
    }
    assert rows[1]["datetime__date"] == datetime.date(2025, 11, 3)
    assert rows[1]["datetime__hour"] == 9


@pytest.mark.asyncio
async def test_parts_follow_the_current_zone(db):
    await create_datetimes()
    with Timezone.override("Europe/Moscow"):
        assert await DatetimeFields.objects.filter(id=1).values_list("datetime__hour", "datetime__date") == [
            (1, datetime.date(2026, 3, 29))
        ]
        assert await DatetimeFields.objects.filter(id=1).values_list("datetime__day", flat=True) == [29]
        assert await DatetimeFields.objects.filter(id=1).values(hour="datetime__hour") == [{"hour": 1}]
    with Timezone.override("UTC"):
        assert await DatetimeFields.objects.filter(id=1).values_list("datetime__hour", flat=True) == [22]


@pytest.mark.asyncio
async def test_order_by_a_part(db):
    await create_datetimes()
    await DatetimeFields.objects.create(id=3, datetime=datetime.datetime(2024, 5, 1, 12, tzinfo=UTC))
    with Timezone.override("UTC"):
        # Hours 22, 9, 12 - by hour, not by the moment.
        assert await DatetimeFields.objects.all().order_by("datetime__hour").values_list("id", flat=True) == [2, 3, 1]
        assert await DatetimeFields.objects.all().order_by("-datetime__month").values_list("id", flat=True) == [
            2,
            3,
            1,
        ]
        # The part orders without being selected, and a model query orders by it too.
        rows = await DatetimeFields.objects.all().order_by("datetime__year")
        assert [row.id for row in rows] == [3, 2, 1]


@pytest.mark.asyncio
async def test_date_and_time_fields(db):
    await DateFields.objects.create(id=1, date=datetime.date(2026, 2, 14))
    await TimeFields.objects.create(id=1, time=datetime.time(18, 45, 30))
    assert await DateFields.objects.all().values("date__year", "date__month", "date__week") == [
        {"date__year": 2026, "date__month": 2, "date__week": 7}
    ]
    assert await TimeFields.objects.all().values_list("time__hour", "time__minute", "time__second") == [(18, 45, 30)]


@pytest.mark.asyncio
async def test_through_relations(db):
    tournament = await Tournament.objects.create(id=1, name="Spring")
    await Tournament.objects.filter(id=1).update(created=LATE_EVENING)
    await Event.objects.create(event_id=1, name="Final", tournament=tournament)
    with Timezone.override("UTC"):
        assert await Event.objects.all().values("name", "tournament__created__year", "tournament__created__date") == [
            {
                "name": "Final",
                "tournament__created__year": 2026,
                "tournament__created__date": datetime.date(2026, 3, 28),
            }
        ]
        assert await Event.objects.all().order_by("-tournament__created__hour").values_list("event_id", flat=True) == [
            1
        ]


@pytest.mark.asyncio
async def test_grouped_by_a_part(db):
    await create_datetimes()
    await DatetimeFields.objects.create(id=3, datetime=datetime.datetime(2026, 7, 1, 12, tzinfo=UTC))
    with Timezone.override("UTC"):
        rows = (
            await DatetimeFields.objects.all()
            .annotate(count=Count("id"))
            .group_by("datetime__year")
            .order_by("datetime__year")
            .values("datetime__year", "count")
        )
    assert rows == [{"datetime__year": 2025, "count": 1}, {"datetime__year": 2026, "count": 2}]


@requires_features(supports_distinct_on=True)
@pytest.mark.asyncio
async def test_distinct_on_a_part(db):
    await create_datetimes()
    await DatetimeFields.objects.create(id=3, datetime=datetime.datetime(2026, 7, 1, 12, tzinfo=UTC))
    with Timezone.override("UTC"):
        ids = (
            await DatetimeFields.objects.all()
            .distinct("datetime__year")
            .order_by("datetime__year", "-id")
            .values_list("id", flat=True)
        )
        annotated_ids = (
            await DatetimeFields.objects.annotate(year=ExtractYear("datetime"))
            .distinct("year")
            .order_by("year", "id")
            .values_list("id", flat=True)
        )
    assert ids == [2, 3]
    assert annotated_ids == [2, 1]


@pytest.mark.asyncio
async def test_a_part_the_field_has_none_of(db):
    with pytest.raises(FieldError):
        await DateFields.objects.all().values("date__hour")
    with pytest.raises(FieldError):
        await TimeFields.objects.all().values("time__year")
    with pytest.raises(FieldError):
        await DateFields.objects.all().order_by("date__hour")
    with pytest.raises(FieldError):
        await Tournament.objects.all().values("name__year")
