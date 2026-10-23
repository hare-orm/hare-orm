from typing import Any, cast

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import RollbackIsolation
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.models import Model
from hare.sql import Table
from tests.utils.attached_databases import get_url_with_attached_databases


class SchemaModel(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "schema_model"
        schema = "schema_one"


class DefaultSchemaModel(Model):
    id = fields.IntField(primary_key=True)


def _get_table(model: type[Model]) -> Table:
    return cast(Any, model).get_table()


@pytest_asyncio.fixture(scope="module")
async def model_get_table_db_module():
    """Fixture for model get_table tests with in-memory SQLite."""
    db_url = get_url_with_attached_databases("sqlite+aiosqlite://:memory:", ["schema_one"])
    async with hare_test_context(modules=[__name__], db_url=db_url) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def model_get_table_db(model_get_table_db_module):
    async with RollbackIsolation(model_get_table_db_module) as context:
        yield context


@pytest.mark.sqlite_only
def test_get_table_returns_fresh_table(model_get_table_db):
    table = _get_table(SchemaModel)

    assert isinstance(table, Table)
    assert table.get_table_name() == SchemaModel._meta.db_table
    assert table._schema is not None
    assert table._schema._name == SchemaModel._meta.schema
    assert table is not SchemaModel._meta.basetable
    assert table is not _get_table(SchemaModel)


def test_get_table_default_schema(model_get_table_db):
    table = _get_table(DefaultSchemaModel)

    assert isinstance(table, Table)
    assert table.get_table_name() == DefaultSchemaModel._meta.db_table
    assert table._schema is None
