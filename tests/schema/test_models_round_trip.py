"""The models of the test modules go round the whole migration cycle on the database under test: their
migrations made and applied, the database then shows no drift and asks for no further migration,
``generate_schemas()`` builds the very schema the migrations built, and unapplying every migration leaves
the database without a table of the models."""

import pytest

from hare import Connections
from hare.core.hare_context import HareContext
from hare.migrations.autodetection.migration_splitter import MigrationSplitter
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.drift import detect_drift
from hare.migrations.migration import Migration
from hare.migrations.state.state import State
from tests.schema.test_generate_schemas_matches_migrate import read_schema
from tests.utils.database_under_test import DatabaseUnderTest

#: The model modules each database is taken round with.
MODULES_BY_DIALECT_NAME = {
    "sqlite": ["tests.shared_models.testmodels"],
    "postgresql": ["tests.shared_models.testmodels", "tests.shared_models.testmodels_postgres"],
    "clickhouse": ["tests.dialects.clickhouse.models"],
}


def get_migrations(models) -> list[Migration]:
    """The migrations makemigrations makes of the models - from nothing, split by app."""
    new_state = State.from_models(models)
    operations_by_app = {
        app_label: OperationGenerator(State(), new_state).generate(app_labels=[app_label])
        for app_label in sorted({HareContext.require_current().apps.get_binding(model).app for model in models})
    }
    split_migrations = MigrationSplitter(State(), list(operations_by_app), lambda app_label: []).split(
        operations_by_app
    )
    return [
        Migration(f"{position:04d}", split_migration.app_label, operations=split_migration.operations)
        for position, split_migration in enumerate(split_migrations)
    ]


@pytest.mark.asyncio
async def test_the_models_go_round_the_migration_cycle():
    modules = MODULES_BY_DIALECT_NAME.get(DatabaseUnderTest.get_dialect().name)
    if modules is None:
        pytest.skip("no model modules for the database under test")

    async with DatabaseUnderTest.open_model_context(modules, generate_schemas=True):
        connection = Connections.get("models")
        editor = connection.dialect.schema_editor_class(connection)
        generated_schema = await read_schema(
            connection, [binding._model for binding in editor.table_creation.get_bindings_to_create()]
        )

    async with DatabaseUnderTest.open_model_context(modules, generate_schemas=False):
        connection = Connections.get("models")
        editor = connection.dialect.schema_editor_class(connection)
        models = [binding._model for binding in editor.table_creation.get_bindings_to_create()]
        app_labels = sorted({HareContext.require_current().apps.get_binding(model).app for model in models})
        assert await read_schema(connection, models) == {}

        state = State()
        states_before: list[State] = []
        migrations = get_migrations(models)
        for migration in migrations:
            states_before.append(state)
            state = await migration.apply(state, schema_editor=editor)

        new_state = State.from_models(models)
        drift = await detect_drift(connection, new_state, app_labels)
        assert (drift.operations, drift.untracked_columns, drift.mismatched_columns) == ([], [], [])
        for app_label in app_labels:
            assert OperationGenerator(state, new_state).generate(app_labels=[app_label]) == [], app_label
        migrated_schema = await read_schema(connection, models)
        assert sorted(migrated_schema) == sorted(generated_schema)
        for table_key, generated_table in generated_schema.items():
            assert migrated_schema[table_key] == generated_table, table_key

        for migration, state_before in zip(reversed(migrations), reversed(states_before), strict=True):
            await migration.unapply(state_before, schema_editor=editor)
        assert await read_schema(connection, models) == {}
