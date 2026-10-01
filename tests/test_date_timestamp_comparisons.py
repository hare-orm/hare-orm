"""A date compared with a timestamp, and naive timestamps (use_tz=False) in SQL conversions."""

import datetime
import os
import uuid
from typing import Any

import pytest

from hare import fields
from hare.contrib.test.helpers import hare_test_context
from hare.query.expressions import F, Value
from hare.query.functions import Cast, Concat
from tests.typed_row_models import TypedRow


def get_test_db_url() -> str:
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url


async def get_ids(**lookup: Any) -> list[int]:
    return sorted(await TypedRow.objects.filter(**lookup).values_list("id", flat=True))


@pytest.mark.asyncio
@pytest.mark.parametrize(("use_tz", "zone"), [(True, "UTC"), (True, "Asia/Tokyo"), (False, "UTC")])
async def test_date_field_compares_with_timestamp_as_its_first_moment(use_tz, zone):
    async with hare_test_context(["tests.typed_row_models"], db_url=get_test_db_url(), use_tz=use_tz, timezone=zone):
        day = datetime.date(2020, 1, 2)
        moments = [
            datetime.datetime(2020, 1, 2),
            datetime.datetime(2020, 1, 2, 0, 30),
            datetime.datetime(2020, 1, 1, 23, 30),
        ]
        for row_id, moment in enumerate(moments, 1):
            await TypedRow.objects.create(id=row_id, day=day, at=moment)
        # The same rows a date literal matches - its day's first moment in the configured zone.
        for lookup in ("", "__not", "__gt", "__gte", "__lt", "__lte"):
            expected = await get_ids(**{f"at{lookup}": day})
            assert await get_ids(**{f"at{lookup}": F("day")}) == expected, lookup
        assert await get_ids(at=F("day")) == [1]
        assert await get_ids(day__lt=F("at")) == [2]
        assert await get_ids(day__gte=F("at")) == [1, 3]
        assert await get_ids(day=F("at")) == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["UTC", "Asia/Tokyo"])
async def test_naive_timestamp_converts_as_its_wall_clock(zone):
    async with hare_test_context(["tests.typed_row_models"], db_url=get_test_db_url(), use_tz=False, timezone=zone):
        await TypedRow.objects.create(
            id=1,
            at=datetime.datetime(2020, 1, 2, 1, 4, 5, 500000),
            day=datetime.date(2020, 1, 2),
            name="2020-01-02 01:30:00",
        )
        [values] = await TypedRow.objects.annotate(
            as_date=Cast("at", fields.DateField()),
            as_text=Cast("at", fields.CharField(max_length=40)),
            as_time=Cast("at", fields.TimeField()),
            from_text=Cast("name", fields.DatetimeField()),
            from_date=Cast("day", fields.DatetimeField()),
            joined=Concat("at", Value("|")),
        ).values_list("as_date", "as_text", "as_time", "from_text", "from_date", "joined")
        assert values == (
            datetime.date(2020, 1, 2),
            "2020-01-02 01:04:05.5",
            datetime.time(1, 4, 5, 500000),
            datetime.datetime(2020, 1, 2, 1, 30),
            datetime.datetime(2020, 1, 2),
            "2020-01-02 01:04:05.500000|",
        )
