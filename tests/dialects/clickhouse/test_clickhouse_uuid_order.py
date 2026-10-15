"""UUIDs order and compare by their 16 bytes, as on PostgreSQL - ClickHouse's own order of a UUID takes
its lower 64 bits first, so a time-ordered key (uuid7) wouldn't sort by time."""

import uuid

import pytest

from hare.exceptions import FieldError
from hare.query.functions import Max
from tests.dialects.clickhouse.models import Team

NUMBERS = {"five": 5, "high": 1 << 100, "three": 3, "middle": 1 << 64, "large": 77, "two": 2}


async def create_teams():
    await Team.objects.bulk_create([Team(id=uuid.UUID(int=number), name=name) for name, number in NUMBERS.items()])


@pytest.mark.asyncio
async def test_uuids_order_by_their_bytes(clickhouse_db):
    await create_teams()
    assert await Team.objects.order_by("id").values_list("name", flat=True) == [
        "two",
        "three",
        "five",
        "large",
        "middle",
        "high",
    ]
    assert (await Team.objects.order_by("-id").first()).name == "high"


@pytest.mark.asyncio
async def test_uuids_compare_by_their_bytes(clickhouse_db):
    await create_teams()
    ten = uuid.UUID(int=10)
    assert sorted(await Team.objects.filter(id__gt=ten).values_list("name", flat=True)) == ["high", "large", "middle"]
    assert sorted(await Team.objects.filter(id__lte=ten).values_list("name", flat=True)) == ["five", "three", "two"]
    assert sorted(
        await Team.objects.filter(id__range=(uuid.UUID(int=3), uuid.UUID(int=100))).values_list("name", flat=True)
    ) == ["five", "large", "three"]


@pytest.mark.asyncio
async def test_a_cursor_walks_uuids_in_their_order(clickhouse_db):
    await create_teams()
    first_page = await Team.objects.order_by("id").limit(2)
    rest = await Team.objects.order_by("id").after_cursor(first_page[-1].id).values_list("name", flat=True)
    assert rest == ["five", "large", "middle", "high"]


@pytest.mark.asyncio
async def test_the_largest_uuid_is_refused_as_on_postgresql(clickhouse_db):
    with pytest.raises(FieldError, match="doesn't take a UUID"):
        await Team.objects.aggregate(top=Max("id"))
