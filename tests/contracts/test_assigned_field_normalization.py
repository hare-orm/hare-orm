"""Fresh keyword and mapping inputs keep field normalization before native assignment."""

import ipaddress

import pytest

from hare import Model, fields
from hare.exceptions import ValidationError
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.utils.trimmed_text_field import TrimmedTextField


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_text_normalization_runs_for_constructor_create_and_mapping_updates(contract_database, composite):
    stored_inputs = []

    class StoredTextField(TrimmedTextField):
        def to_db_value(self, value, instance):
            stored_inputs.append(value)
            return "db:" + super().to_db_value(value, instance)

        def from_db_value(self, value):
            return super().from_db_value(value.removeprefix("db:"))

    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        title = StoredTextField(max_length=30)

        class Meta:
            table = "normalized_book"

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    for repeat in (0, 1):
        obj = Book(number=repeat * 3 + 1, title="  token  ")
        assert obj.title == "token"
        await obj.save()
        created = await Book.objects.create(number=repeat * 3 + 2, title="  token  ")
        assert created.title == "token"
        changed = Book(number=repeat * 3 + 3, title="before")
        changed.update_from_dict({"title": "  token  "})
        assert changed.title == "token"
        await changed.save()
    assert stored_inputs == ["token"] * 6
    connection = contract_database.get_connection()
    assert (
        await connection.execute_dicts("SELECT title FROM normalized_book ORDER BY number")
        == [{"title": "db:token"}] * 6
    )
    assert [obj.title for obj in await Book.objects.order_by("number")] == ["token"] * 6
    assert await Book.objects.order_by("number").values_list("title", flat=True) == ["token"] * 6

    assigned = Book(number=7, title="before")
    assigned.title = "  token  "
    assert assigned.title == "  token  "
    with pytest.raises(ValidationError, match="text must be normalized before writing"):
        await assigned.save()
    with pytest.raises(ValidationError):
        await Book.objects.create(number=7, title="x" * 31)
    assert await connection.execute_dicts("SELECT count(*) AS rows FROM normalized_book") == [{"rows": 6}]


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_ip_text_is_a_python_address_immediately_after_construction(contract_database, composite):
    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        address = fields.IPAddressField()

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    obj = Book(number=1, address="192.0.2.1")
    assert obj.address == ipaddress.IPv4Address("192.0.2.1")
    assert obj.address.version == 4
    await obj.save()
    obj.update_from_dict({"address": "::ffff:192.0.2.2"})
    assert obj.address == ipaddress.IPv4Address("192.0.2.2")
    await obj.save()
    assert (await Book.objects.get(number=1)).address == ipaddress.IPv4Address("192.0.2.2")
