"""An IPv6 address naming a zone (``fe80::1%eth0``): an address column holds no zone - PostgreSQL's ``inet``
and ClickHouse's ``IPv6`` - so it is refused there; SQLite stores the address as its text, zone included."""

import ipaddress

import pytest

from hare.core.hare_context import HareContext
from hare.exceptions import ValidationError
from tests.fields.value_types.models_ip_address import Host


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["fe80::1%eth0", "::ffff:192.0.2.1%eth0"])
@pytest.mark.parametrize("object_input", [False, True], ids=["text", "address"])
async def test_an_address_naming_a_zone_is_kept_as_text_or_refused(db_ordered_values, text, object_input):
    """asyncpg dropped the zone, rust_pg failed with the server's error and ClickHouse read the row's
    address back as None."""
    keeps_text = HareContext.require_current().apps.get_queryset(Host).get_connection().dialect.name == "sqlite"
    value = ipaddress.IPv6Address(text) if object_input else text
    if keeps_text:
        await Host.objects.create(id=1, address=value)
        assert (await Host.objects.get(id=1)).address == ipaddress.IPv6Address(text)
        assert await Host.objects.filter(address=value).count() == 1
        return
    with pytest.raises(ValidationError, match="names a zone"):
        await Host.objects.create(id=1, address=value)
    with pytest.raises(ValidationError, match="names a zone"):
        await Host.objects.filter(address=value).count()
    assert await Host.objects.count() == 0
    await Host.objects.create(id=2, address="fe80::1")
    assert str((await Host.objects.get(id=2)).address) == "fe80::1"
    with pytest.raises(ValidationError, match="names a zone"):
        await Host.objects.filter(id=2).update(address=value)
    obj = await Host.objects.get(id=2)
    obj.address = value
    with pytest.raises(ValidationError, match="names a zone"):
        await obj.save(update_fields=["address"])
    assert str((await Host.objects.get(id=2)).address) == "fe80::1"


@pytest.mark.asyncio
async def test_an_unscoped_mapped_address_still_uses_ipv4(db_ordered_values):
    await Host.objects.create(id=1, address="::ffff:192.0.2.1")
    assert (await Host.objects.get(id=1)).address == ipaddress.IPv4Address("192.0.2.1")
    assert await Host.objects.filter(address="192.0.2.1").count() == 1
