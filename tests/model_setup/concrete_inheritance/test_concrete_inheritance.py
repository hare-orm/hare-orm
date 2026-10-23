"""A concrete model subclassing a concrete model: a table of its own, without the relations to the other."""

import os

import pytest

from hare.contrib.test import hare_test_context
from hare.exceptions import QueryError
from tests.model_setup.concrete_inheritance.concrete_inheritance_models import CornerShelf, Shelf, ShelfItem


@pytest.mark.asyncio
async def test_a_subclass_has_no_relation_another_model_points_at_its_base_with():
    """CornerShelf(id=1).items read the ShelfItem rows of Shelf(id=1), a row of another table, and
    items.add() wrote a CornerShelf key into a relation to Shelf."""
    async with hare_test_context(
        modules=["tests.model_setup.concrete_inheritance.concrete_inheritance_models"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ):
        shelf = await Shelf.objects.create(id=1)
        corner_shelf = await CornerShelf.objects.create(id=1)
        await ShelfItem.objects.create(id=1, shelf=shelf)
        assert await shelf.items.count() == 1
        with pytest.raises(AttributeError, match="items"):
            corner_shelf.items  # noqa: B018
        assert not hasattr(CornerShelf, "items")
        assert await CornerShelf.objects.values_list("corner", flat=True) == [0]


@pytest.mark.asyncio
async def test_a_filter_of_the_relation_to_the_base_refuses_a_subclass_instance():
    """filter(shelf=corner_shelf) compared the CornerShelf key with the Shelf rows' - the Shelf row of that
    key matched - while create(shelf=corner_shelf) was refused."""
    async with hare_test_context(
        modules=["tests.model_setup.concrete_inheritance.concrete_inheritance_models"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ):
        shelf = await Shelf.objects.create(id=1)
        corner_shelf = await CornerShelf.objects.create(id=1)
        await ShelfItem.objects.create(id=1, shelf=shelf)
        assert await ShelfItem.objects.filter(shelf=shelf).count() == 1
        for lookup in ({"shelf": corner_shelf}, {"shelf__in": [corner_shelf]}):
            with pytest.raises(QueryError, match="expects Shelf instances or key values, got a CornerShelf"):
                await ShelfItem.objects.filter(**lookup).count()
        # Built again from the plan of the call with a Shelf.
        with pytest.raises(QueryError, match="got a CornerShelf"):
            await ShelfItem.objects.filter(shelf=corner_shelf).count()
        with pytest.raises(QueryError, match="expects ShelfItem instances or key values, got a Shelf"):
            await Shelf.objects.filter(items=shelf).count()
        assert await Shelf.objects.filter(items=await ShelfItem.objects.get(id=1)).count() == 1
