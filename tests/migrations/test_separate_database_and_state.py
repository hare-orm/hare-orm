"""SeparateDatabaseAndState: state operations change only the migration state, database operations only
the database - applied, unapplied, collected by sqlmigrate, written to a migration file and read back,
and never folded across by a squash."""

from __future__ import annotations

import pytest

from hare import Connections
from hare.exceptions import OperationalError
from hare.fields import CharField, IntField
from hare.migrations.making.migration_optimizer import MigrationOptimizer
from hare.migrations.migration import Migration
from hare.migrations.operations import (
    AddField,
    CreateModel,
    DeleteModel,
    RemoveField,
    RunSQL,
    SeparateDatabaseAndState,
)
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.migrations.writer import MigrationWriter

TABLE = "separate_book"


def make_migration(name: str, *operations) -> Migration:
    migration = Migration(name=name, app_label="models")
    migration.operations = list(operations)
    return migration


async def column_exists(connection, column: str) -> bool:
    quoted_table = connection.dialect.literals.quote_identifier(TABLE)
    quoted_column = connection.dialect.literals.quote_identifier(column)
    try:
        await connection.execute_dicts(f"SELECT {quoted_column} FROM {quoted_table}")
    except OperationalError:
        return False
    return True


@pytest.mark.asyncio
async def test_state_and_database_change_separately(db_simple):
    connection = Connections.get("models")
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    create = make_migration(
        "0001_initial",
        CreateModel(
            name="Book",
            fields=[("id", IntField(primary_key=True)), ("legacy_code", CharField(max_length=10, null=True))],
            options={"table": TABLE},
        ),
    )
    try:
        state = await create.apply(state, schema_editor=editor)
        # The field leaves the models - its column stays.
        state_only = make_migration(
            "0002_forget_legacy_code",
            SeparateDatabaseAndState(state_operations=[RemoveField(model_name="Book", name="legacy_code")]),
        )
        before_state_only = state.clone()
        state = await state_only.apply(state, schema_editor=editor)
        assert "legacy_code" not in state.models[("models", "Book")].fields
        assert await column_exists(connection, "legacy_code")
        # The column is added in the database while the state already knows the field from a later
        # step - and an index made by SQL the state never sees.
        database_only = make_migration(
            "0003_database_side",
            SeparateDatabaseAndState(
                database_operations=[
                    AddField(model_name="Book", name="pages", field=IntField(null=True)),
                    RunSQL(
                        f"CREATE INDEX separate_book_pages ON {TABLE} (pages)",
                        reverse_sql="DROP INDEX separate_book_pages",
                    ),
                ],
                state_operations=[AddField(model_name="Book", name="pages", field=IntField(null=True))],
            ),
        )
        before_database_only = state.clone()
        state = await database_only.apply(state, schema_editor=editor)
        assert "pages" in state.models[("models", "Book")].fields
        assert await column_exists(connection, "pages")

        state = await database_only.unapply(before_database_only, schema_editor=editor)
        assert not await column_exists(connection, "pages")
        await state_only.unapply(before_state_only, schema_editor=editor)
        assert await column_exists(connection, "legacy_code")
    finally:
        await connection.execute_script(f"DROP TABLE IF EXISTS {connection.dialect.literals.quote_identifier(TABLE)}")


@pytest.mark.asyncio
async def test_sqlmigrate_shows_the_database_operations(db_simple):
    connection = Connections.get("models")
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=True)
    state = State(models={}, apps=StateApps())
    state = await make_migration(
        "0001_initial",
        CreateModel(name="Book", fields=[("id", IntField(primary_key=True))], options={"table": TABLE}),
    ).apply(state, schema_editor=editor, collect_sql=True)
    editor.collected_sql.clear()
    await make_migration(
        "0002_separate",
        SeparateDatabaseAndState(
            database_operations=[RunSQL("SELECT 1 /* database step */")],
            state_operations=[AddField(model_name="Book", name="pages", field=IntField(null=True))],
        ),
    ).apply(state, schema_editor=editor, collect_sql=True)
    collected = "\n".join(editor.collected_sql)
    assert "Custom state/database change combination" in collected
    assert "database step" in collected
    assert "pages" not in collected


def test_written_to_a_migration_file_and_read_back():
    operation = SeparateDatabaseAndState(
        database_operations=[RunSQL("SELECT 1", reverse_sql="SELECT 2")],
        state_operations=[RemoveField(model_name="Book", name="legacy_code")],
    )
    source = MigrationWriter("0002_separate", "models", [operation]).as_string()
    migration = Migration.from_source(source, name="0002_separate", app_label="models")
    [read_back] = migration.operations
    assert isinstance(read_back, SeparateDatabaseAndState)
    assert read_back.deconstruct()[2].keys() == {"database_operations", "state_operations"}
    assert isinstance(read_back.database_operations[0], RunSQL)
    assert read_back.database_operations[0].reverse_sql == "SELECT 2"
    assert isinstance(read_back.state_operations[0], RemoveField)
    assert SeparateDatabaseAndState().deconstruct()[2] == {}
    assert not SeparateDatabaseAndState(database_operations=[RunSQL("SELECT 1")]).reversible


def test_a_squash_folds_nothing_across_it():
    operations = [
        CreateModel(name="Book", fields=[("id", IntField(primary_key=True))]),
        SeparateDatabaseAndState(state_operations=[AddField(model_name="Book", name="pages", field=IntField())]),
        DeleteModel(name="Book"),
    ]
    assert MigrationOptimizer("models").optimize(operations) == operations
