"""Address normalization preserves zones until the selected storage validates them."""

import ipaddress

import pytest

from hare import Model, fields
from hare.exceptions import UnSupportedError, ValidationError
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("shape", ["plain", "array", "tuple"])
async def test_mapped_address_zone_follows_the_storage_policy(contract_database, composite, shape):
    declaration = fields.IPAddressField()
    if shape == "array":
        declaration = fields.ArrayField(declaration)
    elif shape == "tuple":
        declaration = fields.TupleField([declaration])
    connection = contract_database.get_connection()
    try:
        declaration.get_column_type(connection.dialect)
    except UnSupportedError:
        pytest.skip("The dialect has no column type for this container")

    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        address = declaration

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    keeps_text = connection.dialect.name == "sqlite"
    for number, address in enumerate(
        ["::ffff:192.0.2.1%eth0", ipaddress.IPv6Address("::ffff:192.0.2.1%eth0")], start=1
    ):
        payload = address if shape == "plain" else [address] if shape == "array" else (address,)
        if keeps_text:
            await Book.objects.create(number=number, address=payload)
            value = (await Book.objects.get(number=number)).address
            if shape != "plain":
                value = value[0]
            assert value == ipaddress.IPv6Address("::ffff:192.0.2.1%eth0")
            assert await Book.objects.filter(number=number, address=payload).count() == 1
        else:
            with pytest.raises(ValidationError, match="names a zone"):
                await Book.objects.create(number=number, address=payload)
            with pytest.raises(ValidationError, match="names a zone"):
                await Book.objects.filter(address=payload).count()
    if not keeps_text:
        assert await Book.objects.count() == 0
    address = "::ffff:192.0.2.1"
    payload = address if shape == "plain" else [address] if shape == "array" else (address,)
    await Book.objects.create(number=3, address=payload)
    value = (await Book.objects.get(number=3)).address
    if shape != "plain":
        value = value[0]
    assert value == ipaddress.IPv4Address("192.0.2.1")
