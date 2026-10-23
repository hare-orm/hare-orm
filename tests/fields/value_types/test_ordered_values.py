"""IP addresses and times of day order, compare and aggregate as each database orders its column: on
PostgreSQL an ``inet`` IPv4 address before every IPv6 one and a ``TIMETZ`` by its UTC time; on SQLite and
ClickHouse a time of day by its stored text, on SQLite an address by its text."""

import datetime

import pytest

from hare.query.functions import Max, Min
from tests.fields.value_types.models_ip_address import Host

PLUS_THREE = datetime.timezone(datetime.timedelta(hours=3))
UTC = datetime.UTC


async def create_hosts():
    await Host.objects.bulk_create(
        [
            Host(id=1, address="::1", opens=datetime.time(10, 0, tzinfo=PLUS_THREE)),
            Host(id=2, address="10.0.0.1", opens=datetime.time(7, 30, tzinfo=UTC)),
            Host(id=3, address="2001:db8::1", opens=datetime.time(9, 0, tzinfo=UTC)),
            Host(id=4, address="0.0.0.0", opens=datetime.time(6, 0, tzinfo=UTC)),
            Host(id=5, address=None, opens=None),
        ]
    )


async def ids(**filters):
    return sorted(await Host.objects.filter(**filters).values_list("id", flat=True))


@pytest.mark.asyncio
async def test_addresses_order_ipv4_first_then_by_bytes(db_ordered_values):
    if Host._meta.connection.dialect.name != "postgresql":
        pytest.skip("PostgreSQL's inet order - SQLite orders the stored text, ClickHouse its IPv6 bytes")
    await create_hosts()
    ordered = await Host.objects.filter(address__isnull=False).order_by("address").values_list("id", flat=True)
    assert ordered == [4, 2, 1, 3]
    assert await ids(address__gt="10.0.0.1") == [1, 3]
    assert await ids(address__lt="::1") == [2, 4]
    assert await ids(address__range=("1.0.0.0", "::1")) == [1, 2]
    assert await Host.objects.aggregate(low=Min("address"), high=Max("address")) == {
        "low": Host._meta.fields_map["address"].to_python("0.0.0.0"),
        "high": Host._meta.fields_map["address"].to_python("2001:db8::1"),
    }
    assert await ids(address__startswith="10.") == [2]


@pytest.mark.asyncio
async def test_times_order_by_their_utc_time(db_ordered_values):
    if Host._meta.connection.dialect.name != "postgresql":
        pytest.skip("PostgreSQL's TIMETZ order - SQLite and ClickHouse order a time of day by its stored text")
    await create_hosts()
    ordered = await Host.objects.filter(opens__isnull=False).order_by("opens").values_list("id", flat=True)
    # 10:00+03:00 is 07:00 UTC.
    assert ordered == [4, 1, 2, 3]
    assert await ids(opens__lt=datetime.time(7, 15, tzinfo=UTC)) == [1, 4]
    # 11:00+03:00 is 08:00 UTC.
    assert await ids(opens__gt=datetime.time(11, 0, tzinfo=PLUS_THREE)) == [3]
    aggregated = await Host.objects.aggregate(low=Min("opens"), high=Max("opens"))
    assert (aggregated["low"], aggregated["high"]) == (
        datetime.time(6, 0, tzinfo=UTC),
        datetime.time(9, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_times_order_by_their_text_where_stored_as_text(db_ordered_values):
    if Host._meta.connection.dialect.name == "postgresql":
        pytest.skip("PostgreSQL orders a TIMETZ by its UTC time")
    await create_hosts()
    ordered = await Host.objects.filter(opens__isnull=False).order_by("opens").values_list("id", flat=True)
    # The ISO texts: 06:00, 07:30, 09:00, 10:00+03:00.
    assert ordered == [4, 2, 3, 1]
