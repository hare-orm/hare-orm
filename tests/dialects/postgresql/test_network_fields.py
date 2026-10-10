"""InetField / CidrField / MacAddressField: inet, cidr and macaddr columns - values written as text or
ipaddress objects and read back as ipaddress objects, subnet lookups, the family and prefix length as
paths, MAC addresses in every usual form - and the values and databases they refuse."""

from __future__ import annotations

import ipaddress

import pytest

from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.fields.network import CidrField, InetField, MacAddressField
from hare.exceptions import UnSupportedError, ValidationError
from hare.query.expressions import F
from tests.dialects.postgresql.models_network import NetworkHost


async def create_hosts() -> None:
    await NetworkHost.objects.create(id=1, address="10.0.0.5", subnet="10.0.0.0/24", mac="08:00:2B:01:02:03")
    await NetworkHost.objects.create(
        id=2, address=ipaddress.ip_interface("10.0.1.7/16"), subnet=ipaddress.ip_network("10.0.0.0/16")
    )
    await NetworkHost.objects.create(id=3, address="2001:db8::1", subnet="2001:db8::/32", mac="0800.2b01.0204")


@pytest.mark.asyncio
async def test_values_are_read_as_ipaddress_objects(db_network):
    await create_hosts()
    hosts = {host.id: host for host in await NetworkHost.objects.all()}
    assert hosts[1].address == ipaddress.IPv4Address("10.0.0.5")
    assert hosts[2].address == ipaddress.IPv4Interface("10.0.1.7/16")
    assert hosts[3].address == ipaddress.IPv6Address("2001:db8::1")
    assert hosts[1].subnet == ipaddress.IPv4Network("10.0.0.0/24")
    assert hosts[3].subnet == ipaddress.IPv6Network("2001:db8::/32")
    assert (hosts[1].mac, hosts[2].mac, hosts[3].mac) == ("08:00:2b:01:02:03", None, "08:00:2b:01:02:04")
    assert await NetworkHost.objects.filter(id=1).values_list("address", "subnet", "mac") == [
        (ipaddress.IPv4Address("10.0.0.5"), ipaddress.IPv4Network("10.0.0.0/24"), "08:00:2b:01:02:03")
    ]


@pytest.mark.asyncio
async def test_equality_and_membership(db_network):
    await create_hosts()
    assert await NetworkHost.objects.filter(address="10.0.0.5").values_list("id", flat=True) == [1]
    assert await NetworkHost.objects.filter(address=ipaddress.ip_address("2001:db8::1")).values_list(
        "id", flat=True
    ) == [3]
    assert await NetworkHost.objects.filter(subnet__in=["10.0.0.0/24", "2001:db8::/32"]).order_by("id").values_list(
        "id", flat=True
    ) == [1, 3]
    assert await NetworkHost.objects.filter(mac="08-00-2b-01-02-03").values_list("id", flat=True) == [1]
    assert await NetworkHost.objects.filter(mac__gt="08:00:2b:01:02:03").values_list("id", flat=True) == [3]


@pytest.mark.asyncio
async def test_subnet_lookups(db_network):
    await create_hosts()

    async def get_ids(**kwargs) -> list[int]:
        return list(await NetworkHost.objects.filter(**kwargs).order_by("id").values_list("id", flat=True))

    assert await get_ids(address__net_contained="10.0.0.0/8") == [1, 2]
    assert await get_ids(address__net_contained=ipaddress.ip_network("10.0.0.0/24")) == [1]
    assert await get_ids(subnet__net_contains="10.0.0.9") == [1, 2]
    assert await get_ids(subnet__net_contains="10.0.0.0/24") == [2]
    assert await get_ids(subnet__net_contains_or_equal="10.0.0.0/24") == [1, 2]
    assert await get_ids(subnet__net_contained_or_equal="10.0.0.0/24") == [1]
    assert await get_ids(subnet__net_overlaps="10.0.0.128/25") == [1, 2]
    # Strictly inside: 10.0.1.7/16 is as wide as its subnet 10.0.0.0/16.
    assert await get_ids(address__net_contained=F("subnet")) == [1, 3]
    assert await get_ids(address__net_contained_or_equal=F("subnet")) == [1, 2, 3]


@pytest.mark.asyncio
async def test_family_and_prefix_length_as_paths(db_network):
    await create_hosts()
    assert await NetworkHost.objects.filter(address__family=6).values_list("id", flat=True) == [3]
    long_prefix_hosts = NetworkHost.objects.filter(subnet__masklen__gte=24).order_by("id")
    assert await long_prefix_hosts.values_list("id", flat=True) == [1, 3]
    assert await NetworkHost.objects.order_by("id").values_list("address__masklen", flat=True) == [32, 16, 128]


@pytest.mark.asyncio
async def test_queryset_update(db_network):
    await create_hosts()
    assert await NetworkHost.objects.filter(id=2).update(address="192.168.0.1", mac="aa:bb:cc:dd:ee:ff") == 1
    host = await NetworkHost.objects.get(id=2)
    assert (host.address, host.mac) == (ipaddress.IPv4Address("192.168.0.1"), "aa:bb:cc:dd:ee:ff")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (InetField(), "10.0.0.300"),
        (InetField(), "not an address"),
        (CidrField(), "10.0.0.1/8"),
        (CidrField(), "2001:db8::1/16"),
        (MacAddressField(), "08:00:2b:01:02"),
        (MacAddressField(), "zz:00:2b:01:02:03"),
        (MacAddressField(), 12345),
    ],
)
def test_a_wrong_value_is_refused(field, value):
    field.model_field_name = "value"
    with pytest.raises(ValidationError):
        field.to_db_value(value, None)


@pytest.mark.asyncio
async def test_a_wrong_subnet_filter_value_is_refused(db_network):
    with pytest.raises(ValidationError, match="neither an address nor a network"):
        await NetworkHost.objects.filter(subnet__net_contains="not an address").count()


def test_a_database_without_the_types_refuses_the_fields():
    sqlite_dialect = DialectRegistry.get_dialect("sqlite")
    for field in (InetField(), CidrField(), MacAddressField()):
        with pytest.raises(UnSupportedError):
            field.get_column_type(sqlite_dialect)
