"""UUIDs order and compare as ClickHouse's own UUID type does - its lower 64 bits first, so a
time-ordered key (uuid7) doesn't sort by time; a cursor walks every row once."""

import uuid

import pytest

from tests.dialects.clickhouse.models import Team

NUMBERS = {"five": 5, "high": 1 << 100, "three": 3, "middle": 1 << 64, "large": 77, "two": 2}


async def create_teams():
    await Team.objects.bulk_create([Team(id=uuid.UUID(int=number), name=name) for name, number in NUMBERS.items()])


@pytest.mark.asyncio
async def test_a_cursor_walks_every_uuid_once(clickhouse_db):
    await create_teams()
    ordered = await Team.objects.order_by("id").values_list("name", flat=True)
    first_page = await Team.objects.order_by("id").limit(2)
    rest = await Team.objects.order_by("id").after_cursor(first_page[-1].id).values_list("name", flat=True)
    assert [team.name for team in first_page] + rest == ordered
    assert sorted(ordered) == sorted(NUMBERS)
