"""Output aliases select their source values without selecting unrelated annotations."""

import pytest

from hare import Model, fields
from hare.query.expressions import F
from hare.query.functions import Count
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_renamed_field_does_not_select_the_annotation_of_its_output_name(contract_database, composite):
    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        category = fields.IntField()
        score = fields.IntField()

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    for number, category in [(1, 1), (2, 1), (3, 2)]:
        await Book.objects.create(number=number, category=category, score=number * 10)
    source = Book.objects.annotate(value=F("score"))
    query = source.values(value="category")
    for __ in range(2):
        assert sorted(row["value"] for row in await query.distinct()) == [1, 2]
        grouped = await query.annotate(total=Count("pk"))
        assert sorted(grouped, key=lambda row: row["value"]) == [
            {"value": 1, "total": 2},
            {"value": 2, "total": 1},
        ]
        assert await query.distinct().count() == 2
        assert sorted(row["value"] for row in await query.filter(value__gte=20)) == [1, 2]
        assert [row["value"] for row in await query.order_by("-value")] == [2, 1, 1]
        assert await source.values(value="category", score="value").order_by("number") == [
            {"value": 1, "score": 10},
            {"value": 1, "score": 20},
            {"value": 2, "score": 30},
        ]
        assert await source.values_list("value", flat=True).order_by("number") == [10, 20, 30]
        swapped = Book.objects.annotate(left=F("score") + 1, right=F("score") + 2)
        assert await swapped.values(left="right", right="left").order_by("number") == [
            {"left": 12, "right": 11},
            {"left": 22, "right": 21},
            {"left": 32, "right": 31},
        ]
        assert sorted(
            await Book.objects.annotate(**{"0": F("score")}).values_list("category", flat=True).distinct()
        ) == [1, 2]
