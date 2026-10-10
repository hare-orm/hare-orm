"""PostgreSQL's side of the safe operations: a foreign key added NOT VALID until ValidateConstraint,
NOT NULL set through a CHECK validated without blocking writes and dropped afterwards, and a table's
rows read off the planner's estimate once it has one."""

from __future__ import annotations

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.ddl.generated_names import GeneratedNames
from hare.fields import CharField, ForeignKeyField, IntField
from hare.migrations.migration import Migration
from hare.migrations.operations import AddField, AlterColumnNotNullSafe, CreateModel, ValidateConstraint
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps

AUTHOR_TABLE = "pg_safe_author"
BOOK_TABLE = "pg_safe_book"
CONSTRAINT_VALIDITY_SQL = (
    "SELECT conname, convalidated FROM pg_constraint WHERE conrelid = '{table}'::regclass ORDER BY 1"
)


def make_migration(name: str, *operations, atomic: bool = True) -> Migration:
    migration = Migration(name=name, app_label="models", operations=list(operations))
    migration.atomic = atomic
    return migration


async def create_tables(connection) -> State:
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    state = await make_migration(
        "0001_initial",
        CreateModel(name="Author", fields=[("id", IntField(primary_key=True))], options={"table": AUTHOR_TABLE}),
        CreateModel(
            name="Book",
            fields=[("id", IntField(primary_key=True)), ("title", CharField(max_length=20, null=True))],
            options={"table": BOOK_TABLE},
        ),
    ).apply(state, schema_editor=editor)
    await connection.execute_script(f"INSERT INTO {AUTHOR_TABLE} (id) VALUES (1)")
    await connection.execute_script(f"INSERT INTO {BOOK_TABLE} (id, title) VALUES (1, 'x'), (2, 'y'), (3, 'z')")
    return state


async def drop_tables(connection) -> None:
    await connection.execute_script(f"DROP TABLE IF EXISTS {BOOK_TABLE}, {AUTHOR_TABLE}")


@requires_features(supports_not_valid_constraints=True)
@pytest.mark.asyncio
async def test_the_foreign_key_stays_not_valid_until_validated(db_simple):
    connection = Connections.get("models")
    try:
        state = await create_tables(connection)
        editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
        foreign_key_name = GeneratedNames.get_foreign_key_name(BOOK_TABLE, ("author_id",), AUTHOR_TABLE, ("id",))
        state = await make_migration(
            "0002_author",
            AddField("Book", "author", ForeignKeyField("models.Author", null=True, db_index=False), not_valid=True),
        ).apply(state, schema_editor=editor)
        validity = await connection.execute_dicts(CONSTRAINT_VALIDITY_SQL.format(table=BOOK_TABLE))
        assert {row["conname"]: row["convalidated"] for row in validity}[foreign_key_name] is False
        await make_migration("0003_validate", ValidateConstraint("Book", foreign_key_name)).apply(
            state, schema_editor=editor
        )
        validity = await connection.execute_dicts(CONSTRAINT_VALIDITY_SQL.format(table=BOOK_TABLE))
        assert {row["conname"]: row["convalidated"] for row in validity}[foreign_key_name] is True
    finally:
        await drop_tables(connection)


@requires_features(supports_not_valid_constraints=True)
@pytest.mark.asyncio
async def test_not_null_without_a_transaction_goes_through_a_check_dropped_afterwards(db_simple):
    connection = Connections.get("models")
    try:
        state = await create_tables(connection)
        migration = make_migration("0002_title", AlterColumnNotNullSafe("Book", "title"), atomic=False)
        collecting_editor = connection.dialect.schema_editor_class(connection, atomic=False, collect_sql=True)
        await migration.apply(state.clone(), schema_editor=collecting_editor, collect_sql=True)
        statements = "\n".join(collecting_editor.collected_sql)
        assert "NOT VALID" in statements
        assert "VALIDATE CONSTRAINT" in statements
        assert "SET NOT NULL" in statements
        assert "DROP CONSTRAINT" in statements

        editor = connection.dialect.schema_editor_class(connection, atomic=False, collect_sql=False)
        await migration.apply(state.clone(), schema_editor=editor)
        constraints = await connection.execute_dicts(CONSTRAINT_VALIDITY_SQL.format(table=BOOK_TABLE))
        assert [row["conname"] for row in constraints if row["conname"].startswith("nn_")] == []
        not_null = await connection.execute_dicts(
            f"SELECT is_nullable FROM information_schema.columns WHERE table_name = '{BOOK_TABLE}' "
            "AND column_name = 'title'"
        )
        assert not_null == [{"is_nullable": "NO"}]
    finally:
        await drop_tables(connection)


@requires_features(supports_not_valid_constraints=True)
@pytest.mark.asyncio
async def test_rows_are_read_off_the_planner_estimate_once_analyzed(db_simple):
    connection = Connections.get("models")
    safety_rules = connection.dialect.migration_safety_rules
    try:
        await create_tables(connection)
        # Never analyzed: counted, no further than asked.
        assert await safety_rules.count_table_rows(connection, BOOK_TABLE, None, 2) == 2
        assert await safety_rules.count_table_rows(connection, BOOK_TABLE, None, 10) == 3
        await connection.execute_script(f"ANALYZE {BOOK_TABLE}")
        assert await safety_rules.count_table_rows(connection, BOOK_TABLE, None, 2) == 3
        assert await safety_rules.count_table_rows(connection, "pg_safe_missing", None, 2) == 0
    finally:
        await drop_tables(connection)
