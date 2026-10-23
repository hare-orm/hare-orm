"""generate_schemas() and the migrations of the same models build the same schema: every table read back
from the catalog - its columns, keys, foreign keys, indexes and constraints - is the same either way."""

import dataclasses
import os
from typing import Any

import pytest

from hare import Connections
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.autodetection.migration_splitter import MigrationSplitter
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.migration import Migration
from hare.migrations.state.state import State

MODULES = ["tests.shared_models.testmodels"]


def get_comparable(value: Any) -> Any:
    """A table read back, in a form that compares whatever order the database lists its parts in."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: get_comparable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {key: get_comparable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return sorted((get_comparable(item) for item in value), key=repr)
    return value


async def read_schema(connection, models) -> dict[tuple[str | None, str], Any]:
    introspector_class = DatabaseCatalog.get_introspector_class(connection)
    default_schema = await introspector_class.fetch_default_schema(connection)
    schemas = {default_schema, *(model._meta.schema for model in models if model._meta.schema)}
    tables: dict[tuple[str | None, str], Any] = {}
    for schema in sorted(schemas):
        table_names = await introspector_class.fetch_table_names(connection, schema, True)
        for table_info in await introspector_class.fetch_tables(connection, table_names, schema):
            tables[(schema, table_info.name)] = get_comparable(table_info)
    return tables


def open_context(generate_schemas: bool):
    return hare_test_context(
        modules=MODULES,
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
        _generate_schemas=generate_schemas,
    )


@pytest.mark.asyncio
async def test_generate_schemas_builds_the_schema_the_migrations_build():
    async with open_context(generate_schemas=True):
        connection = Connections.get("models")
        editor = connection.dialect.schema_editor_class(connection)
        models = editor.table_creation.get_models_to_create()
        generated_schema = await read_schema(connection, models)

    async with open_context(generate_schemas=False):
        connection = Connections.get("models")
        editor = connection.dialect.schema_editor_class(connection)
        models = editor.table_creation.get_models_to_create()
        new_state = State.from_models(models)
        operations_by_app = {
            app_label: OperationGenerator(State(), new_state).generate(app_labels=[app_label])
            for app_label in sorted({model._meta.app for model in models})
        }
        split_migrations = MigrationSplitter(State(), list(operations_by_app), lambda app_label: []).split(
            operations_by_app
        )
        state = State()
        for position, split_migration in enumerate(split_migrations):
            migration = Migration(f"{position:04d}", split_migration.app_label, operations=split_migration.operations)
            await migration.apply(state, schema_editor=editor)
        migrated_schema = await read_schema(connection, models)

    assert sorted(generated_schema) == sorted(migrated_schema)
    for table_key, generated_table in generated_schema.items():
        assert generated_table == migrated_schema[table_key], table_key


@pytest.mark.asyncio
async def test_a_safe_schema_script_runs_again_over_the_schema_it_built():
    async with open_context(generate_schemas=True):
        connection = Connections.get("models")
        script = connection.get_schema_sql(safe=True)
        await connection.execute_script(script)
        await connection.execute_script(script)
