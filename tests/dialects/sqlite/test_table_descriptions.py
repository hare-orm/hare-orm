"""SQLite keeps the text of CREATE TABLE up to its closing parenthesis only - a table's description is
written inside the parentheses, so it stays in the schema, through a rebuild of the table too."""

import pytest

from hare import fields
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.migrations.operations import AlterField, AlterModelOptions, CreateModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps


async def get_table_sql(client: AiosqliteClient) -> str:
    rows = await client.execute_dicts("SELECT sql FROM sqlite_master WHERE name = 'described'")
    return rows[0]["sql"]


@pytest.mark.asyncio
async def test_a_table_description_stays_in_the_schema():
    client = AiosqliteClient(file_path=":memory:", connection_alias="table_descriptions")
    await client.create_connection(with_db=True)
    try:
        editor = client.dialect.schema_editor_class(client, atomic=True, collect_sql=False)
        state = State(models={}, apps=StateApps())
        for operation in (
            CreateModel(
                name="Described",
                fields=[("id", fields.IntField(primary_key=True)), ("name", fields.CharField(max_length=5))],
                options={"table": "described", "table_description": "Rows (with parentheses)"},
            ),
            # Rebuilt for the wider column.
            AlterField(model_name="Described", name="name", field=fields.CharField(max_length=50)),
        ):
            await operation.run("models", state, dry_run=False, state_editor=editor)
            assert "/* Rows (with parentheses) */" in await get_table_sql(client)
        await AlterModelOptions(
            name="Described", options={"table": "described", "table_description": "Other rows"}
        ).run("models", state, dry_run=False, state_editor=editor)
        assert "/* Other rows */" in await get_table_sql(client)
    finally:
        await client.close()
