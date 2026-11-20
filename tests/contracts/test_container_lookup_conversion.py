"""Container predicates retain the compared field's lookup conversion."""

from decimal import Decimal

import pytest

from hare import Model, fields
from hare.exceptions import UnSupportedError
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.utils.lookup_prefix_field import LookupPrefixField


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_array_item_uses_lookup_conversion_without_write_rounding(contract_database, composite):
    prices = fields.ArrayField(fields.DecimalField(max_digits=5, decimal_places=2))
    connection = contract_database.get_connection()
    try:
        prices.get_column_type(connection.dialect)
    except UnSupportedError:
        pytest.skip("The dialect has no array column type")

    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        amounts = prices
        labels = fields.ArrayField(LookupPrefixField(max_length=30))

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Book.objects.create(number=1, amounts=[Decimal("1.23")], labels=["token"])
    for __ in range(2):
        for amount, expected in [(Decimal("1.234"), []), (Decimal("1.23"), [1])]:
            assert await Book.objects.filter(amounts__item=(0, amount)).values_list("number", flat=True) == expected
            assert await Book.objects.filter(amounts__0=amount).values_list("number", flat=True) == expected
        for label in ("find:token", "token"):
            assert await Book.objects.filter(labels__item=(0, label)).values_list("number", flat=True) == [1]
            assert await Book.objects.filter(labels__0=label).values_list("number", flat=True) == [1]
    assert (await Book.objects.get(number=1)).labels == ["token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_map_key_predicates_use_the_key_field_lookup_hook(contract_database, composite):
    mapping = fields.MapField(LookupPrefixField(max_length=30), fields.IntField())
    connection = contract_database.get_connection()
    try:
        mapping.get_column_type(connection.dialect)
    except UnSupportedError:
        pytest.skip("The dialect has no map column type")

    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        labels = mapping

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Book.objects.create(number=1, labels={"token": 7})
    for __ in range(2):
        for label in ("find:token", "token"):
            for lookup, operand in [("has_key", label), ("has_keys", [label]), ("has_any_keys", [label])]:
                assert await Book.objects.filter(**{f"labels__{lookup}": operand}).values_list(
                    "number", flat=True
                ) == [1]
    assert (await Book.objects.get(number=1)).labels == {"token": 7}
