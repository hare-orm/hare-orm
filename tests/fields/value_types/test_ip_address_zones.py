"""An IPv6 address naming a zone (``fe80::1%eth0``): an address column holds no zone - PostgreSQL's ``inet``
and ClickHouse's ``IPv6`` - so it is refused there; SQLite stores the address as its text, zone included."""

import pytest

from hare.exceptions import ValidationError
from tests.fields.value_types.models_ip_address import Host


@pytest.mark.asyncio
async def test_an_address_naming_a_zone_is_kept_as_text_or_refused(db_ordered_values):
    """asyncpg dropped the zone, rust_pg failed with the server's error and ClickHouse read the row's
    address back as None."""
    keeps_text = Host._meta.connection.dialect.name == "sqlite"
    if keeps_text:
        await Host.objects.create(id=1, address="fe80::1%eth0")
        assert str((await Host.objects.get(id=1)).address) == "fe80::1%eth0"
        assert await Host.objects.filter(address="fe80::1%eth0").count() == 1
        return
    with pytest.raises(ValidationError, match="names a zone"):
        await Host.objects.create(id=1, address="fe80::1%eth0")
    with pytest.raises(ValidationError, match="names a zone"):
        await Host.objects.filter(address="fe80::1%eth0").count()
    assert await Host.objects.count() == 0
    await Host.objects.create(id=2, address="fe80::1")
    assert str((await Host.objects.get(id=2)).address) == "fe80::1"
