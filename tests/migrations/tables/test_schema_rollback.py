"""Rolling a schema migration back on a database with schemas."""

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import DatabaseError
from hare.migrations.operations import CreateSchema
from hare.migrations.state.state import State


@requires_features(supports_schemas=True)
@pytest.mark.asyncio
async def test_rolling_back_create_schema_over_a_schema_holding_a_table_fails_and_keeps_it(db_isolated_no_schema):
    """Rolling back CreateSchema drops the schema without CASCADE - a schema holding a table hare
    doesn't know of is the database's to refuse, never dropped with what it holds."""
    connection = db_isolated_no_schema.get_connection()
    editor = connection.dialect.schema_editor_class(connection, atomic=False)
    state = State()
    old_state = state.clone()
    operation = CreateSchema(schema_name="rollback_kept")
    operation.state_forward("models", state)
    await operation.database_forward("models", old_state, state, editor)
    await connection.execute_script('CREATE TABLE "rollback_kept"."outside" (id INT)')
    try:
        with pytest.raises(DatabaseError):
            await operation.database_backward("models", old_state, state, editor)
        rows = await connection.execute_dicts(
            "SELECT COUNT(*) AS count FROM information_schema.tables "
            "WHERE table_schema = 'rollback_kept' AND table_name = 'outside'"
        )
        assert rows[0]["count"] == 1
    finally:
        await connection.execute_script('DROP SCHEMA IF EXISTS "rollback_kept" CASCADE')
