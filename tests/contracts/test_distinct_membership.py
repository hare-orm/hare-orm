"""Membership is tested after DISTINCT ON chooses a representative row."""

import pytest

from hare import Model, fields
from hare.contrib.test import requires_features
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@requires_features(supports_distinct_on=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("descending", [False, True], ids=["ascending", "descending"])
async def test_contains_keeps_distinct_representatives_and_rebinds_keys(contract_database, composite, descending):
    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False, source_field="wire_number")
        if composite:
            region = fields.IntField(default=7, source_field="wire_region")
            pk = fields.CompositePrimaryKey("region", "number")
        category = fields.IntField()
        score = fields.IntField()

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    objects = [
        await Book.objects.create(number=number, category=(number - 1) // 2, score=number) for number in range(1, 5)
    ]
    query = Book.objects.order_by("category", "-score" if descending else "score", "number").distinct("category")
    expected = {2, 4} if descending else {1, 3}
    assert {obj.number for obj in await query} == expected
    for inputs in (objects, list(reversed(objects))):
        for obj in inputs:
            assert await query.contains(obj) is (obj.number in expected)
            assert await Book.objects.contains(obj)
    assert not await query.filter(number__gt=100).contains(objects[0])
