"""Containers of CharEnumField labels order element by element by the labels - ClickHouse stores an
enum by a number; Min/Max of a container are no raw error of a Nullable container; a map keyed by an
enum reads a key path."""

import uuid

import pytest
import pytest_asyncio

from hare.query.functions import Max, Min
from tests.dialects.clickhouse.models import DeviceStatus, Holding

LOW_KEY = uuid.UUID("00000000-0000-0000-ffff-000000000000")
HIGH_KEY = uuid.UUID("11111111-1111-1111-0000-000000000000")
STATUSES = sorted(DeviceStatus, key=lambda status: status.value)


@pytest_asyncio.fixture
async def holdings(clickhouse_db):
    await Holding.objects.bulk_create(
        [
            Holding(id=1, keys=[HIGH_KEY], statuses=[STATUSES[-1]], counts={STATUSES[-1]: 1}, pair=(HIGH_KEY, 1)),
            Holding(id=2, keys=[LOW_KEY], statuses=[STATUSES[0]], counts={STATUSES[0]: 2}, pair=(LOW_KEY, 2)),
        ]
    )


@pytest.mark.asyncio
async def test_containers_order_by_their_values_order(holdings):
    for name in ("statuses", "statuses__0"):
        assert await Holding.objects.order_by(name).values_list("id", flat=True) == [2, 1], name
        assert await Holding.objects.order_by(f"-{name}").values_list("id", flat=True) == [1, 2], name
    assert await Holding.objects.filter(statuses__0__gt=STATUSES[0]).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_min_and_max_of_containers(holdings):
    assert await Holding.objects.aggregate(low_statuses=Min("statuses")) == {"low_statuses": [STATUSES[0]]}
    assert await Holding.objects.aggregate(high_status=Max("statuses__0")) == {"high_status": STATUSES[-1]}
    # A container holds no NULL on ClickHouse - over no rows its empty value.
    assert await Holding.objects.filter(id=0).aggregate(low_keys=Min("keys"), high_counts=Max("counts")) == {
        "low_keys": [],
        "high_counts": {},
    }
    grouped = (
        await Holding.objects.annotate(low_counts=Min("counts")).order_by("id").values_list("low_counts", flat=True)
    )
    assert grouped == [{STATUSES[-1]: 1}, {STATUSES[0]: 2}]


@pytest.mark.asyncio
async def test_a_map_keyed_by_an_enum_reads_a_key_path(holdings):
    key_path = f"counts__{STATUSES[0].value}"
    assert await Holding.objects.filter(**{key_path: 2}).values_list("id", flat=True) == [2]
    assert await Holding.objects.order_by("id").values_list(key_path, flat=True) == [0, 2]
