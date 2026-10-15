"""SQLite alters a relation in place when its key columns and their REFERENCES stay as they are - its own
index dropped or created by a statement of its own - and rebuilds the table otherwise."""

from typing import Any

import pytest

from hare import fields
from hare.ddl.indexes.index import Index
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.migration import Migration
from hare.migrations.operations import CreateModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps

TABLE = "relation_child"


def get_state(child_options: dict[str, Any], on_delete: Any = fields.CASCADE, **relation_options: Any) -> State:
    state = State(models={}, apps=StateApps())
    CreateModel(
        name="Parent", fields=[("id", fields.IntField(primary_key=True))], options={"table": "relation_parent"}
    ).state_forward("models", state)
    CreateModel(
        name="Child",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            (
                "parent",
                fields.ForeignKeyField(
                    "models.Parent", related_name="children", on_delete=on_delete, **relation_options
                ),
            ),
            ("tag", fields.CharField(max_length=10, default="t")),
        ],
        options={"table": TABLE, **child_options},
    ).state_forward("models", state)
    return state


async def migrate(old: State, new: State) -> tuple[list[str], list[str]]:
    client = AiosqliteClient(file_path=":memory:", connection_alias="relation_in_place")
    await client.create_connection(with_db=True)
    try:
        editor = client.dialect.schema_editor_class(client, atomic=True, collect_sql=False)
        empty = State(models={}, apps=StateApps())
        initial = Migration(name="0001", app_label="models", operations=OperationGenerator(empty, old).generate())
        state = await initial.apply(empty.clone(), schema_editor=editor)
        await client.execute_script(
            f"INSERT INTO relation_parent (id) VALUES (1); INSERT INTO {TABLE} (id, parent_id, tag) VALUES (1, 1, 'x')"
        )
        statements: list[str] = []
        run_sql = editor.run_sql

        async def record_and_run(sql: str) -> None:
            statements.append(sql)
            await run_sql(sql)

        editor.run_sql = record_and_run
        change = Migration(name="0002", app_label="models", operations=OperationGenerator(old, new).generate())
        await change.apply(state, schema_editor=editor)
        rows = await client.execute_dicts(f"SELECT name FROM pragma_index_list('{TABLE}') ORDER BY name")
        assert await client.execute_dicts(f"SELECT id, parent_id, tag FROM {TABLE}") == [
            {"id": 1, "parent_id": 1, "tag": "x"}
        ]
        return statements, [row["name"] for row in rows]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_an_index_making_the_relations_own_redundant_drops_it_in_place():
    statements, index_names = await migrate(
        get_state({}),
        # The relation's own index left to the one leading with its column, as makemigrations writes it.
        get_state({"indexes": (Index(fields=("parent", "tag"), name="c_pt"),)}, db_index=False),
    )
    assert not any(f"new__{TABLE}" in statement for statement in statements), statements
    assert any(statement.startswith("DROP INDEX") for statement in statements), statements
    assert index_names == ["c_pt"]


@pytest.mark.asyncio
async def test_a_changed_on_delete_rebuilds_the_table():
    statements, __ = await migrate(get_state({}), get_state({}, on_delete=fields.RESTRICT))
    assert any(f"new__{TABLE}" in statement for statement in statements), statements
