"""Removing a class cannot remove another class registered under the same label."""

import pytest

from hare import Model, fields
from hare.exceptions import ConfigurationError
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_stale_model_classes_do_not_unregister_their_replacements(contract_database, composite):
    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")

        class Meta:
            app = CONTRACT_APP

    previous = Book
    contract_database.register_live_models([previous], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await previous.objects.create(number=1)
    contract_database.unregister_live_models([previous])

    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")

        class Meta:
            app = CONTRACT_APP

    replacement = Book
    contract_database.register_live_models([replacement], CONTRACT_APP, CONTRACT_CONNECTION)

    class Book(Model):
        number = fields.IntField(primary_key=True, generated=False)

        class Meta:
            app = CONTRACT_APP

    for stale in (previous, Book):
        with pytest.raises(ConfigurationError, match="not registered"):
            contract_database.unregister_live_models([stale])
        assert contract_database.get_model(f"{CONTRACT_APP}.Book") is replacement
        assert (await replacement.objects.get(number=1)).number == 1
    contract_database.unregister_live_models([f"{CONTRACT_APP}.Book"])
    contract_database.register_live_models([replacement], CONTRACT_APP, CONTRACT_CONNECTION)
    assert (await replacement.objects.get(number=1)).number == 1
