"""A uniqueness of an array, which ClickHouse keeps none of and hare checks: the arrays compared by their
items - a list is no key of a dict, and an array takes no __in."""

import pytest

from hare.exceptions import IntegrityError
from tests.dialects.clickhouse.models import TaggedSet


@pytest.mark.asyncio
async def test_an_array_held_by_another_row_is_refused(clickhouse_db):
    await TaggedSet.objects.create(id=1, tags=[1, 2])
    with pytest.raises(IntegrityError, match=r"another row holds tags=\[1, 2\]"):
        await TaggedSet.objects.create(id=2, tags=[1, 2])
    await TaggedSet.objects.create(id=3, tags=[2, 1])
    with pytest.raises(IntegrityError, match=r"another row holds tags=\[9\]"):
        await TaggedSet.objects.bulk_create([TaggedSet(id=4, tags=[9]), TaggedSet(id=5, tags=[9])])
    assert await TaggedSet.objects.order_by("id").values_list("id", flat=True) == [1, 3]


@pytest.mark.asyncio
async def test_an_upsert_finds_the_row_by_its_array(clickhouse_db):
    await TaggedSet.objects.create(id=1, tags=[1, 2], label="old")
    await TaggedSet.objects.bulk_create(
        [TaggedSet(id=2, tags=[1, 2], label="new"), TaggedSet(id=3, tags=[3], label="added")],
        on_conflict=["tags"],
        update_fields=["label"],
    )
    assert await TaggedSet.objects.order_by("id").values_list("id", "label") == [(1, "new"), (3, "added")]
