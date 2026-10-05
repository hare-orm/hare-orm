"""A bulk load (``copy()``) of a model with ``Meta.schema`` writes the rows to the model's table in its schema,
not to a table of the same name the connection finds first."""

import pytest

from hare.contrib.test.isolated_contexts import hare_test_context
from tests.utils.attached_databases import get_url_with_attached_databases
from tests.utils.database_under_test import DatabaseUnderTest

SCHEMA_MODULES = ["tests.schema.models_schema_qualified"]


@pytest.mark.asyncio
async def test_a_bulk_load_writes_the_rows_of_a_model_in_its_schema():
    db_url = get_url_with_attached_databases(DatabaseUnderTest.get_url(), ["custom"])
    async with hare_test_context(SCHEMA_MODULES, db_url=db_url) as ctx:
        category_model = ctx.apps.get_model("models", "SchemaCategory")
        product_model = ctx.apps.get_model("models", "SchemaProduct")
        tag_model = ctx.apps.get_model("models", "SchemaTag")
        connection = category_model._meta.connection
        if not (connection.features.supports_copy and connection.features.supports_schemas):
            pytest.skip("a bulk load into a schema of the database's own")
        # A table of the same name the connection's search path finds first.
        await connection.execute_script('CREATE TABLE "category" (id INTEGER PRIMARY KEY, name TEXT)')

        await category_model.objects.bulk_create(
            [category_model(id=1, name="a"), category_model(id=2, name="b")], use_copy=True
        )

        assert await category_model.objects.order_by("id").values_list("name", flat=True) == ["a", "b"]
        _, rows = await connection.execute('SELECT count(*) FROM "category"')
        assert rows[0][0] == 0

        product = await product_model.objects.create(name="p", category_id=1)
        tags = await tag_model.objects.bulk_create([tag_model(id=1, label="x"), tag_model(id=2, label="y")])
        await product.tags.add(*tags)
        assert sorted(await product.tags.all().values_list("label", flat=True)) == ["x", "y"]
