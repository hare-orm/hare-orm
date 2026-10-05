"""A primary key field left to its db_default on ClickHouse, which returns no row of a write: the key could
neither be checked unique nor read back, so the write is refused before anything is written."""

import datetime

import pytest

from hare.exceptions import UnSupportedError
from tests.dialects.clickhouse.models import DayCounter


@pytest.mark.asyncio
async def test_a_key_left_to_its_database_default_is_refused(clickhouse_db):
    with pytest.raises(UnSupportedError, match="DayCounter.day is part of the primary key and left to its db_default"):
        await DayCounter.objects.create(number=1)
    with pytest.raises(UnSupportedError, match="left to its db_default"):
        await DayCounter.objects.bulk_create([DayCounter(number=2)])
    assert await DayCounter.objects.count() == 0
    created = await DayCounter.objects.create(day=datetime.date(2024, 1, 2), number=3)
    assert created.pk == (datetime.date(2024, 1, 2), 3)
