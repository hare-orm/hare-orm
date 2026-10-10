"""truncate_all_models() with Meta.schema/Meta.managed = False models, generate_schemas() with the
same table name in two schemas, and hare_test_context() cleanup after a failed schema generation."""

import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from hare.contrib.test import truncate_all_models
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.connections.connections import Connections
from hare.exceptions import OperationalError
from tests.utils.database_under_test import DatabaseUnderTest

TEST_DB_URL = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
IS_POSTGRES = DatabaseUnderTest.get_dialect().name == "postgresql"
requires_postgres = pytest.mark.skipif(not IS_POSTGRES, reason="needs Postgres schemas")


@pytest.mark.asyncio
async def test_truncate_all_models_empties_schema_qualified_tables():
    async with hare_test_context(
        ["tests.schema.models_schema_qualified"], db_url=TEST_DB_URL, connection_label="models"
    ) as ctx:
        category_model = ctx.apps.get_model("models", "SchemaCategory")
        product_model = ctx.apps.get_model("models", "SchemaProduct")
        tag_model = ctx.apps.get_model("models", "SchemaTag")
        category = await category_model.objects.create(name="books")
        product = await product_model.objects.create(name="novel", category=category)
        tag = await tag_model.objects.create(label="fiction")
        await product.tags.add(tag)

        await truncate_all_models()

        assert await category_model.objects.all().count() == 0
        assert await product_model.objects.all().count() == 0
        assert await tag_model.objects.all().count() == 0
        category = await category_model.objects.create(name="again")
        product = await product_model.objects.create(name="novel", category=category)
        tag = await tag_model.objects.create(label="fiction")
        assert await product.tags.all().count() == 0


@pytest.mark.asyncio
async def test_truncate_all_models_skips_unmanaged_models():
    async with hare_test_context(
        ["tests.schema.models_unmanaged_view"], db_url=TEST_DB_URL, connection_label="models"
    ) as ctx:
        source_model = ctx.apps.get_model("models", "ViewSourceRow")
        view_model = ctx.apps.get_model("models", "ViewRow")
        client = Connections.get("models")
        await client.execute_script('CREATE VIEW "view_row" AS SELECT "id", "name" FROM "view_source_row"')
        await source_model.objects.create(id=1, name="first")
        assert await view_model.objects.all().count() == 1

        await truncate_all_models()

        assert await source_model.objects.all().count() == 0
        assert await view_model.objects.all().count() == 0


@requires_postgres
@pytest.mark.asyncio
async def test_same_table_name_in_two_schemas_generates_and_truncates():
    async with hare_test_context(
        ["tests.schema.models_same_table_other_schema"], db_url=TEST_DB_URL, connection_label="models"
    ) as ctx:
        other_writer_model = ctx.apps.get_model("models", "OtherSchemaWriter")
        other_note_model = ctx.apps.get_model("models", "OtherSchemaNote")
        public_writer_model = ctx.apps.get_model("models", "PublicWriter")
        public_note_model = ctx.apps.get_model("models", "PublicNote")
        other_writer = await other_writer_model.objects.create(id=1, name="other")
        other_note = await other_note_model.objects.create(id=1, writer=other_writer)
        await other_note.readers.add(other_writer)
        public_writer = await public_writer_model.objects.create(id=1, name="public")
        await public_note_model.objects.create(id=1, writer=public_writer)
        await public_note_model.objects.create(id=2, writer=public_writer)

        assert await other_note_model.objects.all().count() == 1
        assert await public_note_model.objects.all().count() == 2
        assert [writer.name for writer in await other_note.readers.all()] == ["other"]

        await truncate_all_models()

        for model in (other_writer_model, other_note_model, public_writer_model, public_note_model):
            assert await model.objects.all().count() == 0


@pytest.mark.asyncio
async def test_failed_schema_generation_drops_the_created_sqlite_database(tmp_path: Path):
    database_path = tmp_path / f"failed_{uuid.uuid4().hex}.sqlite3"

    with pytest.raises(OperationalError):
        async with hare_test_context(
            ["tests.schema.models_failing_schema"], db_url=f"sqlite+aiosqlite://{database_path.as_posix()}"
        ):
            pass

    assert not database_path.exists()


@requires_postgres
@pytest.mark.asyncio
async def test_failed_schema_generation_drops_the_created_postgres_database():
    import asyncpg

    database_name = f"hare_failed_schema_{uuid.uuid4().hex[:12]}"
    db_url = TEST_DB_URL.replace("{}", database_name) if "{}" in TEST_DB_URL else TEST_DB_URL

    with pytest.raises(OperationalError):
        async with hare_test_context(["tests.schema.models_failing_schema"], db_url=db_url):
            pass

    url_parts = urlsplit(db_url)
    connection = await asyncpg.connect(
        host=url_parts.hostname,
        port=url_parts.port or 5432,
        user=url_parts.username,
        password=url_parts.password,
        database="postgres",
    )
    try:
        database_names = await connection.fetch("SELECT datname FROM pg_database WHERE datname = $1", database_name)
    finally:
        await connection.close()
    assert database_names == []
