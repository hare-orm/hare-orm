"""Integration tests for the zero-downtime migration helpers (BackfillColumn,
AlterColumnNotNullSafe) - executed via schema editors on a real database, mirroring
tests/migrations/test_operations_real_db.py's harness."""

from __future__ import annotations

import pytest

from hare.contrib.test import capture_queries
from hare.exceptions import ConfigurationError
from hare.fields import CompositePrimaryKey, ForeignKeyField
from hare.fields.data.numeric import IntField
from hare.fields.data.text import CharField
from hare.migrations.operations import AlterColumnNotNullSafe, BackfillColumn, CreateModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps


def _get_schema_editor(conn):
    """Get the correct backend-specific SchemaEditor for the given connection.

    Mirrors the factory logic in test_operations_real_db.py / MigrationExecutor._schema_editor().
    """
    return conn.dialect.schema_editor_class(conn, atomic=True, collect_sql=False)


def q(name: str) -> str:
    return f'"{name}"'


@pytest.mark.asyncio
async def test_backfill_column_runs_multiple_bounded_batches(db_simple):
    """BackfillColumn must issue several bounded UPDATEs for a dataset bigger than one batch,
    never one giant UPDATE across the whole table - proven here by counting the actual UPDATE
    statements sent to the database."""
    conn = db_simple.get_connection()
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("status", CharField(max_length=20, null=True)),
        ],
        options={"table": "test_backfill_batches"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_backfill_batches")

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        for row_id in range(1, 6):
            await conn.execute_script(f"INSERT INTO {tbl} ({q('id')}) VALUES ({row_id})")

        op = BackfillColumn(model_name="Widget", field_name="status", value="pending", batch_size=2)

        async with capture_queries(using=conn) as counter:
            await op.run("models", state, dry_run=False, state_editor=editor)

        update_queries = [sql for sql in counter.queries if sql.strip().upper().startswith("UPDATE")]
        assert len(update_queries) > 1

        rows = await conn.execute_dicts(f"SELECT {q('status')} FROM {tbl}")
        assert len(rows) == 5
        assert all(row["status"] == "pending" for row in rows)
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_backfill_column_with_callable_value(db_simple):
    """BackfillColumn accepts a callable, evaluated once, instead of a literal."""
    conn = db_simple.get_connection()
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("status", CharField(max_length=20, null=True)),
        ],
        options={"table": "test_backfill_callable"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_backfill_callable")

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('id')}) VALUES (1)")
        await conn.execute_script(f"INSERT INTO {tbl} ({q('id')}) VALUES (2)")

        op = BackfillColumn(model_name="Widget", field_name="status", value=lambda: "computed", batch_size=100)
        await op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('status')} FROM {tbl}")
        assert all(row["status"] == "computed" for row in rows)
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_backfill_column_with_none_value_terminates(db_simple):
    """BackfillColumn(value=lambda: None) must terminate promptly instead of looping forever.

    Regression test: `SET col = NULL WHERE col IS NULL` never changes which rows still satisfy
    `col IS NULL`, so a naive "repeat until rowcount is 0" loop driven only by that WHERE clause
    never sees rowcount hit 0. This is the exact call the docs' dual-write rename recipe (Pattern
    2) uses, so it also must not hang a real migration run.
    """
    conn = db_simple.get_connection()
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("status", CharField(max_length=20, null=True)),
        ],
        options={"table": "test_backfill_none_value"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_backfill_none_value")

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        for row_id in range(1, 6):
            await conn.execute_script(f"INSERT INTO {tbl} ({q('id')}) VALUES ({row_id})")
        await conn.execute_script(f"UPDATE {tbl} SET {q('status')} = 'kept' WHERE {q('id')} = 5")

        op = BackfillColumn(model_name="Widget", field_name="status", value=lambda: None, batch_size=2)
        await op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('id')}, {q('status')} FROM {tbl} ORDER BY {q('id')}")
        assert [row["status"] for row in rows] == [None, None, None, None, "kept"]
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.parametrize("batch_size", [0, -1])
def test_backfill_column_rejects_non_positive_batch_size(batch_size):
    """batch_size=0 silently no-ops (LIMIT 0 backfills nothing) and a negative batch_size means
    "no limit" on SQLite or a raw DB error on Postgres - both must be rejected at construction
    time instead of surfacing as confusing runtime behavior."""
    with pytest.raises(ConfigurationError):
        BackfillColumn(model_name="Widget", field_name="status", value="pending", batch_size=batch_size)


@pytest.mark.asyncio
async def test_alter_column_not_null_safe_raises_configuration_error_when_nulls_remain(db_simple):
    """AlterColumnNotNullSafe must raise a clear ConfigurationError - not a raw DB error - when a
    NULL still exists in the target column."""
    conn = db_simple.get_connection()
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Config",
        fields=[
            ("id", IntField(primary_key=True)),
            ("value", CharField(max_length=20, null=True)),
        ],
        options={"table": "test_notnull_raises"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_notnull_raises")

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('id')}) VALUES (1)")

        alter_op = AlterColumnNotNullSafe(model_name="Config", field_name="value")
        with pytest.raises(ConfigurationError):
            await alter_op.run("models", state, dry_run=False, state_editor=editor)
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_column_not_null_safe_locks_table_on_postgres_inside_transaction(db_simple):
    """On Postgres, when the null-check and the ALTER run against a real open transaction - as
    the migration executor does for an atomic=True migration, the default - AlterColumnNotNullSafe
    must take a LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE before the check, closing the TOCTOU
    gap where a concurrent writer could otherwise insert a NULL row between the check and the
    ALTER. Not exercised on SQLite, which has no equivalent table-level lock."""
    from hare.transactions.transactions import Transactions

    conn = db_simple.get_connection()
    if conn.dialect.name != "postgresql":
        pytest.skip("LOCK TABLE protection is Postgres-only")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Config",
        fields=[
            ("id", IntField(primary_key=True)),
            ("value", CharField(max_length=20, null=True)),
        ],
        options={"table": "test_notnull_locks_table"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_notnull_locks_table")

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('id')}, {q('value')}) VALUES (1, 'present')")

        alter_op = AlterColumnNotNullSafe(model_name="Config", field_name="value")

        async with Transactions.atomic("models") as tx_conn:
            editor.client = tx_conn
            async with capture_queries(using=tx_conn) as counter:
                await alter_op.run("models", state, dry_run=False, state_editor=editor)

        lock_queries = [sql for sql in counter.queries if sql.strip().upper().startswith("LOCK TABLE")]
        assert len(lock_queries) == 1
        assert "SHARE ROW EXCLUSIVE" in lock_queries[0]
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_column_not_null_safe_succeeds_once_backfilled(db_simple):
    """AlterColumnNotNullSafe succeeds cleanly once every row has a value, and the column
    genuinely enforces NOT NULL afterward."""
    conn = db_simple.get_connection()
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Config",
        fields=[
            ("id", IntField(primary_key=True)),
            ("value", CharField(max_length=20, null=True)),
        ],
        options={"table": "test_notnull_succeeds"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_notnull_succeeds")

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('id')}) VALUES (1)")
        await conn.execute_script(f"INSERT INTO {tbl} ({q('id')}, {q('value')}) VALUES (2, 'present')")

        backfill_op = BackfillColumn(model_name="Config", field_name="value", value="default", batch_size=10)
        await backfill_op.run("models", state, dry_run=False, state_editor=editor)

        alter_op = AlterColumnNotNullSafe(model_name="Config", field_name="value")
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        with pytest.raises(Exception):
            await conn.execute_script(f"INSERT INTO {tbl} ({q('id')}) VALUES (3)")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_backfill_column_state_forward_is_a_pure_noop():
    """BackfillColumn only changes data, never schema state - state_forward() must leave the
    migration state (the in-memory schema model) byte-identical."""
    state = State(models={}, apps=StateApps())
    CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("status", CharField(max_length=20, null=True)),
        ],
    ).state_forward("models", state)

    model_state = state.models[("models", "Widget")]
    fields_before = dict(model_state.fields)
    options_before = dict(model_state.options)
    pk_before = model_state.pk_field_name

    op = BackfillColumn(model_name="Widget", field_name="status", value="pending")
    op.state_forward("models", state)

    assert state.models[("models", "Widget")] is model_state
    assert model_state.fields == fields_before
    assert all(model_state.fields[name] is field for name, field in fields_before.items())
    assert model_state.options == options_before
    assert model_state.pk_field_name == pk_before


@pytest.mark.asyncio
async def test_backfill_column_accepts_a_foreign_key_relation_name(db_simple):
    """field_name may name the relation itself ('author'), not only its key column field
    ('author_id') - it used to fail with a raw KeyError."""
    conn = db_simple.get_connection()
    editor = _get_schema_editor(conn)
    state = State(models={}, apps=StateApps())
    author_table = q("test_backfill_fk_author")
    book_table = q("test_backfill_fk_book")
    create_author = CreateModel(
        name="BackfillAuthor",
        fields=[("id", IntField(primary_key=True))],
        options={"table": "test_backfill_fk_author"},
    )
    create_book = CreateModel(
        name="BackfillBook",
        fields=[
            ("id", IntField(primary_key=True)),
            ("author", ForeignKeyField("models.BackfillAuthor", related_name="books", null=True)),
        ],
        options={"table": "test_backfill_fk_book"},
    )

    try:
        await create_author.run("models", state, dry_run=False, state_editor=editor)
        await create_book.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {author_table} ({q('id')}) VALUES (7)")
        await conn.execute_script(f"INSERT INTO {book_table} ({q('id')}) VALUES (1)")
        await conn.execute_script(f"INSERT INTO {book_table} ({q('id')}) VALUES (2)")

        await BackfillColumn(model_name="BackfillBook", field_name="author", value=7).run(
            "models", state, dry_run=False, state_editor=editor
        )

        rows = await conn.execute_dicts(f"SELECT {q('author_id')} FROM {book_table}")
        assert [row["author_id"] for row in rows] == [7, 7]

        with pytest.raises(ConfigurationError, match="BackfillBook.missing"):
            await BackfillColumn(model_name="BackfillBook", field_name="missing", value=7).run(
                "models", state, dry_run=False, state_editor=editor
            )
    finally:
        for table in (book_table, author_table):
            try:
                await conn.execute_script(f"DROP TABLE IF EXISTS {table}")
            except Exception:
                pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key_fields", "options"),
    [
        ([("pk", CompositePrimaryKey("tenant", "number"))], {}),
        ([], {"primary_key": None}),
    ],
    ids=["composite key", "no key"],
)
async def test_backfill_column_without_a_single_key_column(db_simple, key_fields, options):
    """The batches of a model with a composite primary key, or with none, are picked by the rows
    themselves - there is no single key column to pick them by."""
    conn = db_simple.get_connection()
    editor = _get_schema_editor(conn)
    tbl = q("test_backfill_without_key_column")
    create_op = CreateModel(
        name="Reading",
        fields=[
            ("tenant", IntField()),
            ("number", IntField()),
            ("status", CharField(max_length=20, null=True)),
            *key_fields,
        ],
        options={"table": "test_backfill_without_key_column", **options},
    )
    state = State(models={}, apps=StateApps())
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        for tenant in (1, 2):
            for number in (1, 2, 3):
                await conn.execute_script(
                    f"INSERT INTO {tbl} ({q('tenant')}, {q('number')}) VALUES ({tenant}, {number})"
                )
        op = BackfillColumn(model_name="Reading", field_name="status", value="pending", batch_size=4)
        await op.run("models", state, dry_run=False, state_editor=editor)
        rows = await conn.execute_dicts(f"SELECT {q('status')} FROM {tbl}")
        assert [row["status"] for row in rows] == ["pending"] * 6
    finally:
        await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")


def test_backfill_of_a_model_without_a_key_needs_a_name_of_a_row():
    """A database whose dialect names no row (no ctid, no rowid) refuses to backfill a model
    without a primary key in batches - it couldn't pick a batch's rows."""
    from types import SimpleNamespace

    from hare.dialects.base.schema.columns.column_backfill import ColumnBackfill
    from tests.primary_keyless_models import VisitNote

    editor = SimpleNamespace(client=SimpleNamespace(dialect="third_party"), quote=lambda name: f'"{name}"')
    with pytest.raises(ConfigurationError, match=r"VisitNote\.text in batches - the model has no primary key"):
        ColumnBackfill(editor).get_backfill_key_sql(VisitNote, "text")
