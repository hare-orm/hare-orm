"""AddField of a NOT NULL column whose only default is a Python-level ``default=`` on a table
that already holds rows - existing rows are filled with the default before NOT NULL applies."""

from __future__ import annotations

import datetime

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import IntegrityError, OperationalError
from hare.fields.data.numeric import IntField
from hare.fields.data.temporal import DatetimeField, TimeField
from hare.fields.data.text import CharField
from hare.migrations.operations import AddField, CreateModel
from hare.migrations.state.project import State, StateApps


def _get_schema_editor(conn):
    return conn.dialect.schema_editor_class(conn, atomic=True, collect_sql=False)


def q(name: str) -> str:
    return f'"{name}"'


async def _create_table_with_rows(conn, editor, table: str, row_count: int) -> State:
    create_op = CreateModel(
        name="Thing",
        fields=[("id", IntField(primary_key=True)), ("name", CharField(max_length=20))],
        options={"table": table},
    )
    state = State(models={}, apps=StateApps())
    await create_op.run("models", state, dry_run=False, state_editor=editor)
    for row_id in range(1, row_count + 1):
        await conn.execute_script(f"INSERT INTO {q(table)} ({q('id')}, {q('name')}) VALUES ({row_id}, 'n{row_id}')")
    return state


async def _drop(conn, table: str) -> None:
    try:
        await conn.execute_script(f"DROP TABLE IF EXISTS {q(table)}")
    except Exception:
        pass


@pytest.mark.asyncio
async def test_add_not_null_field_with_python_default_fills_existing_rows(db_simple):
    conn = db_simple.db()
    editor = _get_schema_editor(conn)
    table = "test_add_nn_default_rows"
    try:
        state = await _create_table_with_rows(conn, editor, table, 2)
        add_op = AddField(model_name="Thing", name="note", field=CharField(max_length=10, default="x'y"))
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('note')} FROM {q(table)} ORDER BY {q('id')}")
        assert [row["note"] for row in rows] == ["x'y", "x'y"]
        with pytest.raises((IntegrityError, OperationalError)):
            await conn.execute_script(
                f"INSERT INTO {q(table)} ({q('id')}, {q('name')}, {q('note')}) VALUES (3, 'n3', NULL)"
            )
    finally:
        await _drop(conn, table)


@pytest.mark.asyncio
async def test_add_not_null_field_with_python_default_on_empty_table(db_simple):
    conn = db_simple.db()
    editor = _get_schema_editor(conn)
    table = "test_add_nn_default_empty"
    try:
        state = await _create_table_with_rows(conn, editor, table, 0)
        add_op = AddField(model_name="Thing", name="count", field=IntField(default=7))
        await add_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {q(table)} ({q('id')}, {q('name')}, {q('count')}) VALUES (1, 'a', 1)")
        rows = await conn.execute_dicts(f"SELECT {q('count')} FROM {q(table)}")
        assert rows == [{"count": 1}]
    finally:
        await _drop(conn, table)


@pytest.mark.asyncio
async def test_add_not_null_field_with_callable_default_evaluates_it_once(db_simple):
    conn = db_simple.db()
    editor = _get_schema_editor(conn)
    table = "test_add_nn_default_callable"
    calls: list[int] = []

    def next_value() -> int:
        calls.append(1)
        return 40 + len(calls)

    try:
        state = await _create_table_with_rows(conn, editor, table, 3)
        add_op = AddField(model_name="Thing", name="count", field=IntField(default=next_value))
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('count')} FROM {q(table)}")
        assert [row["count"] for row in rows] == [41, 41, 41]
        assert len(calls) == 1
    finally:
        await _drop(conn, table)


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_add_unique_not_null_field_with_python_default_keeps_uniqueness(db_simple):
    conn = db_simple.db()
    editor = _get_schema_editor(conn)
    table = "test_add_nn_default_unique"
    try:
        state = await _create_table_with_rows(conn, editor, table, 1)
        add_op = AddField(model_name="Thing", name="code", field=CharField(max_length=10, default="c", unique=True))
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('code')} FROM {q(table)}")
        assert rows == [{"code": "c"}]
        with pytest.raises((IntegrityError, OperationalError)):
            await conn.execute_script(
                f"INSERT INTO {q(table)} ({q('id')}, {q('name')}, {q('code')}) VALUES (2, 'n2', 'c')"
            )
    finally:
        await _drop(conn, table)


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_now_kwargs", [{"auto_now_add": True}, {"auto_now": True}])
async def test_add_not_null_auto_now_datetime_field_fills_existing_rows_with_now(db_simple, auto_now_kwargs):
    """An auto_now/auto_now_add DatetimeField has no default=, but its column still can't be added
    NOT NULL to a table with rows - existing rows get the current time, stored exactly as a
    regular save() stores it."""
    conn = db_simple.db()
    editor = _get_schema_editor(conn)
    table = "test_add_nn_auto_now"
    field = DatetimeField(**auto_now_kwargs)
    try:
        state = await _create_table_with_rows(conn, editor, table, 2)
        before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=5)
        add_op = AddField(model_name="Thing", name="stamped", field=field)
        await add_op.run("models", state, dry_run=False, state_editor=editor)
        after = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=5)

        rows = await conn.execute_dicts(f"SELECT {q('stamped')} FROM {q(table)}")
        assert len(rows) == 2
        for row in rows:
            stored = row["stamped"]
            if isinstance(stored, str):
                assert stored == datetime.datetime.fromisoformat(stored).astimezone(datetime.UTC).isoformat(" ")
                stored = datetime.datetime.fromisoformat(stored)
            assert before <= stored <= after
        with pytest.raises((IntegrityError, OperationalError)):
            await conn.execute_script(
                f"INSERT INTO {q(table)} ({q('id')}, {q('name')}, {q('stamped')}) VALUES (3, 'n3', NULL)"
            )
    finally:
        await _drop(conn, table)


@pytest.mark.asyncio
async def test_add_not_null_auto_now_time_field_fills_existing_rows(db_simple):
    conn = db_simple.db()
    if conn.dialect.name != "postgresql":
        pytest.skip("TimeField values are not supported by sqlite3")
    editor = _get_schema_editor(conn)
    table = "test_add_nn_auto_now_time"
    try:
        state = await _create_table_with_rows(conn, editor, table, 1)
        add_op = AddField(model_name="Thing", name="stamped", field=TimeField(auto_now=True))
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('stamped')} FROM {q(table)}")
        assert len(rows) == 1
        assert isinstance(rows[0]["stamped"], datetime.time)
    finally:
        await _drop(conn, table)
