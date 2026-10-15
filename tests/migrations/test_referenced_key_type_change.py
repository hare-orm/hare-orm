"""A migration changing the type of a key other tables reference: their foreign keys are dropped and
created again under the names they had - the SQL ``sqlmigrate`` collects is the SQL the migration
runs, one ``DROP`` for each constraint."""

from __future__ import annotations

from typing import Any

import pytest

from hare import Connections, fields
from hare.contrib.test import requires_features
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.migration import Migration
from hare.migrations.operations import CreateModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps

TABLES = ("keyed_parent", "keyed_child", "pair_parent", "pair_child")


def get_state(code_length: int) -> State:
    state = State(models={}, apps=StateApps())
    models: list[tuple[str, list[tuple[str, Any]], dict[str, Any]]] = [
        ("KeyedParent", [("code", fields.CharField(max_length=code_length, primary_key=True))], {}),
        (
            "KeyedChild",
            [
                ("id", fields.IntField(primary_key=True)),
                ("parent", fields.ForeignKeyField("models.KeyedParent", related_name="children")),
            ],
            {},
        ),
        (
            "PairParent",
            [
                ("number", fields.IntField()),
                ("code", fields.CharField(max_length=code_length)),
                ("pk", fields.CompositePrimaryKey("number", "code")),
            ],
            {},
        ),
        (
            "PairChild",
            [
                ("id", fields.IntField(primary_key=True)),
                ("parent", fields.ForeignKeyField("models.PairParent", related_name="children")),
            ],
            {},
        ),
    ]
    for (name, model_fields, options), table in zip(models, TABLES, strict=True):
        CreateModel(name=name, fields=model_fields, options={"table": table, **options}).state_forward("models", state)
    return state


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_referencing_foreign_key_keeps_its_name(db_simple):
    connection = Connections.get("models")

    def get_editor(collect_sql: bool):
        return connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=collect_sql)

    async def get_foreign_key_names() -> list[str]:
        rows = await connection.execute_dicts(
            "SELECT conname FROM pg_constraint WHERE contype = 'f' "
            "AND conrelid::regclass::text IN ('keyed_child', 'pair_child') ORDER BY conname"
        )
        return [row["conname"] for row in rows]

    empty = State(models={}, apps=StateApps())
    narrow, wide = get_state(10), get_state(30)
    initial = Migration(name="0001", app_label="models", operations=OperationGenerator(empty, narrow).generate())
    state = await initial.apply(empty.clone(), schema_editor=get_editor(collect_sql=False))
    try:
        names = await get_foreign_key_names()
        widen = Migration(name="0002", app_label="models", operations=OperationGenerator(narrow, wide).generate())
        collecting_editor = get_editor(collect_sql=True)
        await widen.apply(state.clone(), schema_editor=collecting_editor)
        collected = [sql for sql in collecting_editor.collected_sql if "CONSTRAINT" in sql]
        running_editor = get_editor(collect_sql=False)
        ran: list[str] = []
        run_sql = running_editor.run_sql

        async def record_and_run(sql: str) -> None:
            ran.append(sql)
            await run_sql(sql)

        running_editor.run_sql = record_and_run
        await widen.apply(state, schema_editor=running_editor)
        assert collected == [sql for sql in ran if "CONSTRAINT" in sql]
        assert sum("DROP CONSTRAINT" in sql for sql in collected) == 2, collected
        assert await get_foreign_key_names() == names
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS " + ", ".join(reversed(TABLES)) + " CASCADE")
