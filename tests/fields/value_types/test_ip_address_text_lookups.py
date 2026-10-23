"""The text lookups of an IP address match the address's own text on every database."""

import pytest

from tests.fields.value_types.models_ip_address import Host


@pytest.mark.asyncio
async def test_the_text_lookups_of_an_address_match_its_text_without_a_mask(db_ordered_values):
    """PostgreSQL matched the text of its inet cast - '1.2.3.4/32' - so endswith and iexact missed the
    address and contains '/' matched every one."""
    await Host.objects.create(id=1, address="1.2.3.4")
    await Host.objects.create(id=2, address="::1")

    async def matching(**filters):
        return await Host.objects.filter(**filters).order_by("id").values_list("id", flat=True)

    assert await matching(address__endswith="3.4") == [1]
    assert await matching(address__iendswith="::1") == [2]
    assert await matching(address__iexact="1.2.3.4") == [1]
    assert await matching(address__startswith="1.2") == [1]
    assert await matching(address__contains="/") == []
    assert await matching(address__contains="32") == []
