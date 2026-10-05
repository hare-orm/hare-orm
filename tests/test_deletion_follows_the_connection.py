"""Whether the database enforces a relation is a fact of the connection a model writes to, not of
the model: a model bound to a database with foreign keys and then to one without them has its
cascade run by hare there."""

import pytest

import tests.dialects.columnar  # noqa: F401 - registers the columnar driver
from hare import fields
from hare.contrib.test import hare_test_context
from hare.models import Model
from hare.models.deletion.cascade.deletion_graph import DeletionGraph

pytestmark = pytest.mark.database_independent

MODELS_MODULE = "tests.test_deletion_follows_the_connection"


class Shelf(Model):
    id = fields.IntField(primary_key=True, generated=False)


class Volume(Model):
    id = fields.IntField(primary_key=True, generated=False)
    shelf = fields.ForeignKeyField("models.Shelf", related_name="volumes")


async def delete_a_shelf_with_volumes() -> list[int]:
    shelf = await Shelf.objects.create(id=1)
    await Volume.objects.create(id=1, shelf=shelf)
    await Volume.objects.create(id=2, shelf=shelf)
    await Shelf.objects.filter(id=1).delete()
    return await Volume.objects.all().values_list("id", flat=True)


@pytest.mark.asyncio
async def test_cascade_runs_by_hare_once_the_model_writes_to_a_database_without_foreign_keys():
    async with hare_test_context([MODELS_MODULE], db_url="sqlite+aiosqlite://:memory:"):
        assert not DeletionGraph.needs_python_cascade(Shelf)
        assert await delete_a_shelf_with_volumes() == []

    async with hare_test_context([MODELS_MODULE], db_url="columnar://:memory:"):
        assert DeletionGraph.needs_python_cascade(Shelf)
        assert await delete_a_shelf_with_volumes() == []
