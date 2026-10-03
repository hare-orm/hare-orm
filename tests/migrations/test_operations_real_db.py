"""Integration tests: Hare migration operations executed via schema editors on real databases.

These tests verify that migration operations (CreateModel, AddField, AlterField,
AddConstraint, RemoveConstraint, etc.) work end-to-end when executed through
their backend-specific schema editors against a real database.

All tests use the ``db_isolated`` fixture for full per-test database isolation.
Each test:
  1. Builds migration state (ModelState / State / StateApps)
  2. Instantiates the correct backend SchemaEditor via the executor factory
  3. Calls ``operation.run(app_label, state, dry_run=False, state_editor=editor)``
  4. Verifies the database state via raw SQL queries
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hare.contrib.test import requires_features
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.enums import TriggerEvent, TriggerForEach, TriggerTiming
from hare.ddl.triggers import Trigger
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.constants import RESTRICT
from hare.fields.data.boolean import BooleanField
from hare.fields.data.numeric import BigIntField, DecimalField, FloatField, IntField, PositiveIntField, SmallIntField
from hare.fields.data.temporal import DatetimeField
from hare.fields.data.text import CharField, TextField
from hare.fields.db_defaults import Now
from hare.fields.generated import GeneratedField
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.exceptions import FieldNarrowingDataLossError
from hare.migrations.migration import Migration
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    AddTrigger,
    AlterField,
    AlterModelSchema,
    AlterModelTable,
    AlterTrigger,
    CreateModel,
    CreateSchema,
    DeleteModel,
    DropSchema,
    RemoveConstraint,
    RemoveField,
    RemoveTrigger,
    RenameField,
    RenameModel,
    RenameTrigger,
)
from hare.migrations.state.project import ModelState, State, StateApps
from hare.models import Model
from hare.query.expressions import Q
from tests.utils.database_under_test import DatabaseUnderTest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_schema_editor(conn):
    """Get the correct backend-specific SchemaEditor for the given connection.

    Mirrors the factory logic in MigrationExecutor._schema_editor().
    """
    return conn.dialect.schema_editor_class(conn, atomic=True, collect_sql=False)


def q(name: str, dialect: str) -> str:
    """Quote an identifier for the given dialect."""
    return f'"{name}"'


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_model_and_delete_model(db_simple):
    """CreateModel creates a real table; DeleteModel drops it."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("name", CharField(max_length=100)),
        ],
        options={"table": "test_widget"},
    )
    state = State(models={}, apps=StateApps())

    try:
        # Forward: create the table
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        # Verify table exists by inserting and querying (let auto-increment assign id)
        tbl = q("test_widget", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('name', dialect)}) VALUES ('gizmo')")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 1
        assert rows[0]["name"] == "gizmo"

        # Backward: delete the table
        delete_op = DeleteModel(name="Widget")
        await delete_op.run("models", state, dry_run=False, state_editor=editor)

        # Verify table no longer exists
        with pytest.raises(Exception):
            await conn.execute_dicts(f"SELECT * FROM {tbl}")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_widget', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_model_table_renames_the_real_table(db_simple):
    """AlterModelTable (a Meta.table-only rename, same Python class name) must actually rename
    the table in the database, not just update in-memory state - complements
    test_generate_alter_model_table_for_table_only_change in test_state_diff_operations.py,
    which only checks that the autodetector emits this operation at all."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True))],
        options={"table": "test_alter_model_table_old"},
    )
    state = State(models={}, apps=StateApps())
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        alter_op = AlterModelTable(name="Widget", table="test_alter_model_table_new")
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_alter_model_table_new", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} (id) VALUES (1)")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 1

        old_tbl = q("test_alter_model_table_old", dialect)
        with pytest.raises(Exception):
            await conn.execute_dicts(f"SELECT * FROM {old_tbl}")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_alter_model_table_old', dialect)}")
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_alter_model_table_new', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_model_table_backward_renames_to_old_table(db_simple):
    """AlterModelTable.database_backward must rename the CURRENT (new) table back to the OLD
    one - not repeat the same forward rename. Regression test for a bug where
    database_backward swapped its old_state/new_state args before calling
    _apply_rename_table(), double-negating the executor's already-correct backward ordering
    and making rollback issue the identical forward RENAME TABLE (or fail outright, since the
    "old" table no longer exists after the forward rename)."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    old_name = "test_alter_model_table_bwd_old"
    new_name = "test_alter_model_table_bwd_new"
    try:
        create_op = CreateModel(
            name="Widget",
            fields=[("id", IntField(primary_key=True))],
            options={"table": old_name},
        )
        state = State(models={}, apps=StateApps())
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        # Migration.unapply() takes the PRE-migration state (before this migration's operations
        # ran) and re-derives the forward states internally - so keep a clone of it separately
        # from the post-migration state used for apply(), mirroring how MigrationExecutor.migrate()
        # calls unapply() with the state projected up to (but not including) this migration.
        pre_migration_state = state.clone()

        migration = Migration(name="0002_alter_table", app_label="models")
        migration.operations = [AlterModelTable(name="Widget", table=new_name)]

        state = await migration.apply(state, dry_run=False, schema_editor=editor)

        new_tbl = q(new_name, dialect)
        await conn.execute_script(f"INSERT INTO {new_tbl} (id) VALUES (1)")

        await migration.unapply(pre_migration_state, dry_run=False, schema_editor=editor)

        old_tbl = q(old_name, dialect)
        rows = await conn.execute_dicts(f"SELECT * FROM {old_tbl}")
        assert len(rows) == 1
        assert rows[0]["id"] == 1

        with pytest.raises(Exception):
            await conn.execute_dicts(f"SELECT * FROM {new_tbl}")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(old_name, dialect)}")
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(new_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_model_schema_moves_the_real_table(db_simple):
    """AlterModelSchema must actually relocate the table via `ALTER TABLE ... SET SCHEMA` - a
    Meta.schema change used to fall into the generic AlterModelOptions diff, whose
    database_forward/backward are unconditional no-ops, so the autodetector-generated migration
    looked like it moved the table but silently left it (and its data) behind in the old schema
    while the ORM's own state believed it had already moved."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("Meta.schema/ALTER TABLE ... SET SCHEMA is Postgres-only")
    editor = _get_schema_editor(conn)

    schema_name = "test_alter_model_schema_target"
    try:
        create_op = CreateModel(
            name="Widget",
            fields=[("id", IntField(primary_key=True))],
            options={"table": "test_alter_model_schema_widget"},
        )
        state = State(models={}, apps=StateApps())
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_alter_model_schema_widget", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} (id) VALUES (1)")

        create_schema_op = CreateSchema(schema_name=schema_name)
        await create_schema_op.run("models", state, dry_run=False, state_editor=editor)

        alter_op = AlterModelSchema(name="Widget", schema=schema_name)
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        # The table is GONE from the default/public schema...
        with pytest.raises(Exception):
            await conn.execute_dicts(f"SELECT * FROM {tbl}")

        # ...and its data survived the move, reachable under the new schema.
        moved_tbl = f'"{schema_name}"."test_alter_model_schema_widget"'
        rows = await conn.execute_dicts(f"SELECT * FROM {moved_tbl}")
        assert len(rows) == 1
        assert rows[0]["id"] == 1
    finally:
        try:
            await conn.execute_script(f'DROP TABLE IF EXISTS "{schema_name}"."test_alter_model_schema_widget"')
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_alter_model_schema_widget', dialect)}")
            drop_schema_op = DropSchema(schema_name=schema_name)
            await drop_schema_op.database_forward(
                "models", State(models={}, apps=StateApps()), State(models={}, apps=StateApps()), editor
            )
        except Exception:
            pass


@pytest.mark.asyncio
async def test_autodetect_and_apply_field_unique_and_unique_together_together(db_simple):
    """Adding a field's own unique=True AND a matching single-field unique_together entry for
    that same field in the SAME change used to autodetect into an AlterField AND an AddConstraint
    - both create the identical UNIQUE constraint (same deterministic name), so applying the
    generated migration crashed with a duplicate-constraint error on Postgres (masked on SQLite,
    whose alter_field rebuilds the whole table). The redundant AddConstraint must be skipped, and
    the migration must apply cleanly end-to-end through the real autodetector."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("the duplicate-constraint crash only manifests on a real in-place ALTER backend")
    editor = _get_schema_editor(conn)

    table_name = "test_uniq_collision_user"
    try:
        create_op = CreateModel(
            name="User",
            fields=[("id", IntField(primary_key=True)), ("email", CharField(max_length=64))],
            options={"table": table_name},
        )
        state = State(models={}, apps=StateApps())
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        OldUser = type(
            "User",
            (Model,),
            {
                "id": IntField(primary_key=True),
                "email": CharField(max_length=64),
                "Meta": type("Meta", (), {"table": table_name, "app": "models"}),
                "_no_comments": True,
            },
        )
        NewUser = type(
            "User",
            (Model,),
            {
                "id": IntField(primary_key=True),
                "email": CharField(max_length=64, unique=True),
                "Meta": type(
                    "Meta",
                    (),
                    {"table": table_name, "app": "models", "constraints": [UniqueConstraint(fields=("email",))]},
                ),
                "_no_comments": True,
            },
        )
        old_state = State(models={}, apps=StateApps())
        old_state.models[("models", "User")] = ModelState.make_from_model("models", OldUser)
        new_state = State(models={}, apps=StateApps())
        new_state.models[("models", "User")] = ModelState.make_from_model("models", NewUser)
        operations = OperationGenerator(old_state, new_state).generate()
        assert not any(op.__class__.__name__ == "AddConstraint" for op in operations)

        migration = Migration(name="0002_add_unique", app_label="models")
        migration.operations = operations
        await migration.apply(state, dry_run=False, schema_editor=editor)

        tbl = q(table_name, dialect)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('email', dialect)}) VALUES ('a@example.com')")
        with pytest.raises(Exception):
            await conn.execute_script(f"INSERT INTO {tbl} ({q('email', dialect)}) VALUES ('a@example.com')")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_field_backward_restores_the_old_nullability_via_real_unapply(db_simple):
    """AlterField's own state/database_backward interplay, exercised through Migration.unapply()
    (not a manually-constructed reverse AlterField the way test_alter_field_null_change above
    does) - proves unapply() itself correctly derives the OLD field definition to pass to
    database_backward(), the same real-executor path test_alter_model_table_backward_renames_to_
    old_table above already proved for AlterModelTable (which caught a real swapped-args bug)."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect == "sqlite":
        pytest.skip("SQLite does not support ALTER COLUMN nullability changes")
    editor = _get_schema_editor(conn)

    table_name = "test_alter_field_unapply_config"
    create_op = CreateModel(
        name="Config",
        fields=[
            ("id", IntField(primary_key=True)),
            ("value", IntField(null=False, default=0)),
        ],
        options={"table": table_name},
    )
    state = State(models={}, apps=StateApps())
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        pre_migration_state = state.clone()

        migration = Migration(name="0002_alter_field", app_label="models")
        migration.operations = [AlterField(model_name="Config", name="value", field=IntField(null=True))]

        state = await migration.apply(state, dry_run=False, schema_editor=editor)

        tbl = q(table_name, dialect)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('value', dialect)}) VALUES (NULL)")
        await conn.execute_script(f"UPDATE {tbl} SET {q('value', dialect)} = 0 WHERE {q('value', dialect)} IS NULL")

        await migration.unapply(pre_migration_state, dry_run=False, schema_editor=editor)

        with pytest.raises(Exception):
            await conn.execute_script(f"INSERT INTO {tbl} ({q('value', dialect)}) VALUES (NULL)")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_field_class_change_without_an_automatic_cast_still_lands(db_simple):
    """AlterField's own ALTER COLUMN TYPE used to have no USING clause at all - Postgres only
    accepts a type change without one when an implicit/assignment cast exists between the two
    types (a narrow subset), and rejects everything else with DatatypeMismatchError, even a
    change that DOES have a valid EXPLICIT cast. Confirmed live before this fix: TextField ->
    IntField on a column holding genuinely numeric text raised DatatypeMismatchError on both
    Postgres drivers. Now emits `... TYPE integer USING "value"::integer`, which Postgres
    accepts and correctly converts the existing rows."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect == "sqlite":
        pytest.skip("SQLite rebuilds the whole table for any ALTER FIELD - no USING clause involved")
    editor = _get_schema_editor(conn)

    table_name = "test_alter_field_cast_widget"
    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("value", TextField(null=True)),
        ],
        options={"table": table_name},
    )
    state = State(models={}, apps=StateApps())
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q(table_name, dialect)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('value', dialect)}) VALUES ('42'), ('7')")

        migration = Migration(name="0002_alter_field", app_label="models")
        migration.operations = [AlterField(model_name="Widget", name="value", field=IntField(null=True))]
        await migration.apply(state, dry_run=False, schema_editor=editor)

        _, rows = await conn.execute(f"SELECT {q('value', dialect)} FROM {tbl} ORDER BY {q('id', dialect)}")
        assert [dict(row)["value"] for row in rows] == [42, 7]
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_field_backward_restores_the_old_name_via_real_unapply(db_simple):
    """RenameField's own state/database_backward interplay, exercised through Migration.unapply()
    - proves unapply() correctly derives the OLD field name/state to reverse the rename, the same
    real-executor path already proven for AlterModelTable above."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    table_name = "test_rename_field_unapply_widget"
    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("old_name", IntField(null=True)),
        ],
        options={"table": table_name},
    )
    state = State(models={}, apps=StateApps())
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        pre_migration_state = state.clone()

        migration = Migration(name="0002_rename_field", app_label="models")
        migration.operations = [
            RenameField(model_name="Widget", old_name="old_name", new_name="new_name", field=IntField(null=True))
        ]

        state = await migration.apply(state, dry_run=False, schema_editor=editor)

        tbl = q(table_name, dialect)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('new_name', dialect)}) VALUES (5)")

        await migration.unapply(pre_migration_state, dry_run=False, schema_editor=editor)

        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 1
        assert rows[0]["old_name"] == 5
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_add_field_with_db_default(db_simple):
    """AddField with db_default=42 creates a column whose default is applied on INSERT."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    # Step 1: Create the table via CreateModel
    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("name", CharField(max_length=100)),
        ],
        options={"table": "test_product"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        # Step 2: Add a field with db_default via AddField
        add_op = AddField(
            model_name="Product",
            name="stock",
            field=IntField(null=False, db_default=42),
        )
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        # Step 3: Insert without specifying stock — should get default
        tbl = q("test_product", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('name', dialect)}) VALUES ('widget')")

        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 1
        assert rows[0]["stock"] == 42
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_add_indexed_field_creates_a_real_index(db_simple):
    """The autodetector now emits a companion AddIndex right after AddField for a new
    index=True/db_index=True field (see StateFieldDiff.generate_operations) - this exercises the
    DDL side of that fix end to end: AddField alone must NOT create an index, and the companion
    AddIndex the autodetector now generates alongside it must."""
    from hare.ddl.indexes import Index

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
        ],
        options={"table": "test_indexed_widget"},
    )
    state = State(models={}, apps=StateApps())

    async def index_exists(table: str, index_name: str) -> bool:
        if dialect == "sqlite":
            rows = await conn.execute_dicts(f"PRAGMA index_list({q(table, dialect)})")
            return any(row["name"] == index_name for row in rows)
        rows = await conn.execute_dicts(
            f"SELECT indexname FROM pg_indexes WHERE tablename = '{table}' AND indexname = '{index_name}'"
        )
        return len(rows) == 1

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        name_field = CharField(max_length=50, db_index=True)
        add_field_op = AddField(model_name="Widget", name="name", field=name_field)
        await add_field_op.run("models", state, dry_run=False, state_editor=editor)

        index = Index(fields=("name",))
        widget_model = state.models[("models", "Widget")].render(state.apps)
        index_name = editor._index_name_for_model(widget_model, index)
        assert not await index_exists("test_indexed_widget", index_name)

        add_index_op = AddIndex(model_name="Widget", index=index)
        await add_index_op.run("models", state, dry_run=False, state_editor=editor)

        assert await index_exists("test_indexed_widget", index_name)
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_indexed_widget', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_second_makemigrations_after_indexed_field_addition_does_not_drop_the_index(db_simple):
    """End-to-end regression test for the AddField+AddIndex tracked-state corruption bug:
    applying an AddField(db_index=True)+AddIndex pair against a real DB, then diffing the resulting
    tracked state against the live model with OperationGenerator, must propose ZERO further
    operations - not a RemoveIndex that would, if applied, actually drop the index the model
    still declares (confirmed here via PRAGMA index_list, mirroring the live catalog check the
    bug was originally caught with)."""
    from hare.ddl.indexes import Index
    from hare.fields import CharField as LiveCharField
    from hare.models import Model as LiveModel

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    async def index_exists(table: str, index_name: str) -> bool:
        if dialect == "sqlite":
            rows = await conn.execute_dicts(f"PRAGMA index_list({q(table, dialect)})")
            return any(row["name"] == index_name for row in rows)
        rows = await conn.execute_dicts(
            f"SELECT indexname FROM pg_indexes WHERE tablename = '{table}' AND indexname = '{index_name}'"
        )
        return len(rows) == 1

    table = "test_index_survives_second_makemigrations"
    try:
        create_op = CreateModel(
            name="Widget",
            fields=[("id", IntField(primary_key=True))],
            options={"table": table},
        )
        tracked_state = State(models={}, apps=StateApps())
        await create_op.run("models", tracked_state, dry_run=False, state_editor=editor)

        name_field = CharField(max_length=50, db_index=True)
        await AddField(model_name="Widget", name="name", field=name_field).run(
            "models", tracked_state, dry_run=False, state_editor=editor
        )
        index = Index(fields=("name",))
        await AddIndex(model_name="Widget", index=index).run(
            "models", tracked_state, dry_run=False, state_editor=editor
        )

        widget_model = tracked_state.models[("models", "Widget")].render(tracked_state.apps)
        index_name = editor._index_name_for_model(widget_model, index)
        assert await index_exists(table, index_name)

        # The live model the user actually declares - same shape as the migrated-to state.
        # (A plain nested `class Meta: table = table` can't see the enclosing function's
        # `table` local through the class-body scope, so build Meta via type() instead.)
        Widget = type(
            "Widget",
            (LiveModel,),
            {
                "name": LiveCharField(max_length=50, db_index=True),
                "Meta": type("Meta", (), {"table": table, "app": "models"}),
            },
        )

        live_state = State(models={}, apps=StateApps())
        live_state.models[("models", "Widget")] = ModelState.make_from_model("models", Widget)

        operations = OperationGenerator(tracked_state, live_state).generate()
        assert operations == []

        assert await index_exists(table, index_name)
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_add_field_with_now_default(db_simple):
    """AddField with db_default=Now() creates a timestamp column with a server-side default."""

    def _parse_timestamp(raw_value) -> datetime:
        # datetime.fromisoformat() accepts both the "T"-separated and space-separated forms,
        # with or without a fractional-seconds component (e.g. SQLite's Now() db_default
        # emits a space-separated form with milliseconds), so a single call covers every
        # shape a driver could hand back here.
        if isinstance(raw_value, str):
            ts = datetime.fromisoformat(raw_value)
            return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
        if isinstance(raw_value, datetime):
            return raw_value if raw_value.tzinfo else raw_value.replace(tzinfo=UTC)
        pytest.fail(f"Unexpected timestamp type: {type(raw_value)}")

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Event",
        fields=[
            ("id", IntField(primary_key=True)),
            ("title", CharField(max_length=100)),
        ],
        options={"table": "test_event"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        # Add a datetime field with Now() default
        add_op = AddField(
            model_name="Event",
            name="created_at",
            field=DatetimeField(null=True, db_default=Now()),
        )
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        # Bracket the insert with the DATABASE's own clock (CURRENT_TIMESTAMP, same connection),
        # not the Python client's - comparing against Python's datetime.now(UTC) was flaky under
        # a full-suite run because client/server clock skew (real, even if small) plus the extra
        # wall-clock delay a busy full run adds between these calls and the actual INSERT could
        # exceed the old 2-second tolerance. Two server-side readings around the same INSERT are
        # bounded only by genuine transaction timing, not clock drift, so no slack is needed at
        # all in practice - keep a tiny one anyway for timestamp-precision rounding.
        before_rows = await conn.execute_dicts("SELECT CURRENT_TIMESTAMP as now")
        before = _parse_timestamp(before_rows[0]["now"])

        tbl = q("test_event", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('title', dialect)}) VALUES ('launch')")

        after_rows = await conn.execute_dicts("SELECT CURRENT_TIMESTAMP as now")
        after = _parse_timestamp(after_rows[0]["now"])

        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 1
        raw_value = rows[0]["created_at"]
        assert raw_value is not None, "Now() default should populate the timestamp"

        ts = _parse_timestamp(raw_value)

        assert (ts - before).total_seconds() >= -1
        assert (after - ts).total_seconds() >= -1
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_event', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_field_set_and_drop_db_default(db_simple):
    """AlterField can SET DEFAULT and then DROP DEFAULT on a real database.

    The table is created without a db_default, then AlterField is used to
    add and subsequently remove the default — this is purely testing the
    AlterField SET/DROP DEFAULT code path.
    """
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)

    if dialect == "sqlite":
        pytest.skip("SQLite does not support ALTER COLUMN SET/DROP DEFAULT")

    editor = _get_schema_editor(conn)

    # Create table WITHOUT db_default — the column is nullable so inserts
    # without a value succeed even before a default is set.
    # A "tag" column is used to distinguish rows instead of explicit id values,
    # so that auto-increment / IDENTITY works on every backend.
    create_op = CreateModel(
        name="Setting",
        fields=[
            ("id", IntField(primary_key=True)),
            ("tag", CharField(max_length=50)),
            ("value", IntField(null=True)),
        ],
        options={"table": "test_setting"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_setting", dialect)

        # AlterField: set default to 99
        alter_op = AlterField(
            model_name="Setting",
            name="value",
            field=IntField(null=True, db_default=99),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        # Insert without specifying value — should get default 99
        await conn.execute_script(f"INSERT INTO {tbl} ({q('tag', dialect)}) VALUES ('with_default')")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl} WHERE {q('tag', dialect)} = 'with_default'")
        assert rows[0]["value"] == 99

        # AlterField: drop default
        drop_op = AlterField(
            model_name="Setting",
            name="value",
            field=IntField(null=True),
        )
        await drop_op.run("models", state, dry_run=False, state_editor=editor)

        # Insert with explicit NULL for value — verifies default no longer applies.
        # We use explicit NULL rather than omitting the column because MySQL in
        # strict mode rejects omitted nullable columns after DROP DEFAULT.
        await conn.execute_script(
            f"INSERT INTO {tbl} ({q('tag', dialect)}, {q('value', dialect)}) VALUES ('no_default', NULL)"
        )
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl} WHERE {q('tag', dialect)} = 'no_default'")
        assert rows[0]["value"] is None
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_setting', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field(db_simple):
    """RemoveField drops a column from a real table."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)

    if dialect == "sqlite":
        pytest.skip("SQLite does not support DROP COLUMN in older versions")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Article",
        fields=[
            ("id", IntField(primary_key=True)),
            ("title", CharField(max_length=200)),
            ("subtitle", CharField(max_length=200)),
        ],
        options={"table": "test_article"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        # Verify column exists
        tbl = q("test_article", dialect)
        await conn.execute_script(
            f"INSERT INTO {tbl} ({q('title', dialect)}, {q('subtitle', dialect)}) VALUES ('Hello', 'World')"
        )

        # Remove the subtitle field
        remove_op = RemoveField(model_name="Article", name="subtitle")
        await remove_op.run("models", state, dry_run=False, state_editor=editor)

        # Verify column is gone — querying it should fail
        with pytest.raises(Exception):
            await conn.execute_dicts(f"SELECT {q('subtitle', dialect)} FROM {tbl}")

        # But the table and other columns still work
        rows = await conn.execute_dicts(f"SELECT {q('title', dialect)} FROM {tbl}")
        assert len(rows) == 1
        assert rows[0]["title"] == "Hello"
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_article', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_model(db_simple):
    """RenameModel renames a table in the real database.

    Uses default table names (lowercased model name) because RenameModel only
    renames the underlying table when the table name matches the default
    convention (old_name.lower() -> new_name.lower()).  Custom table names are
    intentionally left unchanged by the operation.
    """
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    # Use default table naming (lowercased model name, no custom table option)
    create_op = CreateModel(
        name="OldName",
        fields=[
            ("id", IntField(primary_key=True)),
            ("value", IntField(null=False, default=0)),
        ],
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        # Insert data under old name (default table = "oldname")
        old_tbl = q("oldname", dialect)
        await conn.execute_script(f"INSERT INTO {old_tbl} ({q('value', dialect)}) VALUES (42)")

        # Rename: table should change from "oldname" to "newname"
        rename_op = RenameModel(old_name="OldName", new_name="NewName")
        await rename_op.run("models", state, dry_run=False, state_editor=editor)

        # Query via new table name
        new_tbl = q("newname", dialect)
        rows = await conn.execute_dicts(f"SELECT * FROM {new_tbl}")
        assert len(rows) == 1
        assert rows[0]["value"] == 42

        # Old table name should not exist
        with pytest.raises(Exception):
            await conn.execute_dicts(f"SELECT * FROM {old_tbl}")
    finally:
        for tbl_name in ("oldname", "newname"):
            try:
                await conn.execute_script(f"DROP TABLE IF EXISTS {q(tbl_name, dialect)}")
            except Exception:
                pass


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_add_and_remove_unique_constraint(db_simple):
    """AddConstraint creates a unique constraint; RemoveConstraint drops it."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    from hare.exceptions import IntegrityError

    create_op = CreateModel(
        name="Employee",
        fields=[
            ("id", IntField(primary_key=True)),
            ("email", CharField(max_length=200)),
        ],
        options={"table": "test_employee"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_employee", dialect)

        # Add unique constraint via AddConstraint operation
        constraint = UniqueConstraint(fields=("email",), name="uq_employee_email")
        add_constraint_op = AddConstraint(model_name="Employee", constraint=constraint)
        await add_constraint_op.run("models", state, dry_run=False, state_editor=editor)

        # Insert first row
        await conn.execute_script(f"INSERT INTO {tbl} ({q('email', dialect)}) VALUES ('alice@test.com')")

        # Duplicate should fail
        with pytest.raises(IntegrityError):
            await conn.execute_script(f"INSERT INTO {tbl} ({q('email', dialect)}) VALUES ('alice@test.com')")

        # Remove the constraint
        remove_constraint_op = RemoveConstraint(model_name="Employee", name="uq_employee_email")
        await remove_constraint_op.run("models", state, dry_run=False, state_editor=editor)

        # Now duplicates should be allowed
        await conn.execute_script(f"INSERT INTO {tbl} ({q('email', dialect)}) VALUES ('alice@test.com')")

        rows = await conn.execute_dicts(f"SELECT * FROM {tbl} WHERE {q('email', dialect)} = 'alice@test.com'")
        assert len(rows) == 2
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_employee', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_add_deferrable_unique_constraint_defers_check_to_commit(db_simple):
    """A UniqueConstraint(deferrable=True, initially_deferred=True) postpones its uniqueness
    check to COMMIT instead of enforcing it immediately after each statement - proven here by
    inserting a temporarily-duplicate row inside one transaction and fixing it before commit,
    something only DEFERRABLE allows to succeed. A plain (non-deferrable) UniqueConstraint on the
    exact same shape of data, by contrast, rejects the exact same temporary duplicate immediately
    at INSERT time, never even reaching the fix-up UPDATE - confirming the deferral, not some
    unrelated laxity, is what makes the difference. This is the live behavioral proof that
    DEFERRABLE actually works, not just that the generated DDL text looks right."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("DEFERRABLE unique constraints are Postgres-only")
    editor = _get_schema_editor(conn)

    from hare.exceptions import IntegrityError
    from hare.transactions.transactions import Transactions

    create_op = CreateModel(
        name="Order",
        fields=[
            ("id", IntField(primary_key=True)),
            ("room", CharField(max_length=50)),
            ("day", CharField(max_length=50)),
        ],
        options={"table": "test_deferrable_unique"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        constraint = UniqueConstraint(
            fields=("room", "day"),
            name="uq_order_room_day",
            deferrable=True,
            initially_deferred=True,
        )
        add_op = AddConstraint(model_name="Order", constraint=constraint)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_deferrable_unique", dialect)

        await conn.execute_script(f"INSERT INTO {tbl} (id, room, day) VALUES (1, 'A', 'Mon')")

        # Inside one transaction: create a temporarily-duplicate (room, day) pair, then fix it
        # to be unique BEFORE commit - only possible because the uniqueness check is deferred.
        async with Transactions.atomic("models") as tx_conn:
            await tx_conn.execute(f"INSERT INTO {tbl} (id, room, day) VALUES (2, 'A', 'Mon')")
            await tx_conn.execute(f"UPDATE {tbl} SET day = 'Tue' WHERE id = 2")

        rows = await conn.execute_dicts(f"SELECT id, room, day FROM {tbl} ORDER BY id")
        assert rows == [
            {"id": 1, "room": "A", "day": "Mon"},
            {"id": 2, "room": "A", "day": "Tue"},
        ]

        # Contrast: the SAME temporary duplicate against a plain (non-deferrable) UniqueConstraint
        # is rejected immediately at INSERT time, never even reaching the fix-up UPDATE.
        remove_op = RemoveConstraint(model_name="Order", name="uq_order_room_day")
        await remove_op.run("models", state, dry_run=False, state_editor=editor)
        immediate_constraint = UniqueConstraint(fields=("room", "day"), name="uq_order_room_day_immediate")
        add_immediate_op = AddConstraint(model_name="Order", constraint=immediate_constraint)
        await add_immediate_op.run("models", state, dry_run=False, state_editor=editor)

        with pytest.raises(IntegrityError):
            async with Transactions.atomic("models") as tx_conn:
                await tx_conn.execute(f"INSERT INTO {tbl} (id, room, day) VALUES (3, 'A', 'Mon')")
                await tx_conn.execute(f"UPDATE {tbl} SET day = 'Wed' WHERE id = 3")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_deferrable_unique', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_deferred_unique_violation_at_commit_raises_integrity_error(db_simple):
    """Companion to test_add_deferrable_unique_constraint_defers_check_to_commit above: this
    time the temporary (room, day) duplicate is left UNRESOLVED all the way to commit - proving
    commit() itself now raises hare.exceptions.IntegrityError (translate_exceptions is newly
    applied to commit()/rollback() on all three backend clients) instead of leaking the raw
    driver exception (asyncpg.exceptions.UniqueViolationError / rust.pg.IntegrityViolationError)
    straight past hare's own exception hierarchy - previously left as a known gap while adding
    DEFERRABLE support, since only a deferred constraint check can fail at COMMIT rather than at
    the INSERT/UPDATE that violated it."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("DEFERRABLE unique constraints are Postgres-only")
    editor = _get_schema_editor(conn)

    from hare.exceptions import IntegrityError
    from hare.transactions.transactions import Transactions

    create_op = CreateModel(
        name="Order",
        fields=[
            ("id", IntField(primary_key=True)),
            ("room", CharField(max_length=50)),
            ("day", CharField(max_length=50)),
        ],
        options={"table": "test_deferred_commit_violation"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        constraint = UniqueConstraint(
            fields=("room", "day"),
            name="uq_order_room_day_commit",
            deferrable=True,
            initially_deferred=True,
        )
        add_op = AddConstraint(model_name="Order", constraint=constraint)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_deferred_commit_violation", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} (id, room, day) VALUES (1, 'A', 'Mon')")

        with pytest.raises(IntegrityError):
            async with Transactions.atomic("models") as tx_conn:
                await tx_conn.execute(f"INSERT INTO {tbl} (id, room, day) VALUES (2, 'A', 'Mon')")
                # Deliberately left unresolved - the deferred uniqueness check only actually
                # fires at COMMIT, which the `async with` block's own clean-exit __aexit__
                # triggers right here.

        # A failed COMMIT aborts the whole transaction (standard Postgres behavior) - the
        # never-actually-committed id=2 row must not be visible afterward.
        rows = await conn.execute_dicts(f"SELECT id, room, day FROM {tbl} ORDER BY id")
        assert rows == [{"id": 1, "room": "A", "day": "Mon"}]
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_deferred_commit_violation', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_deferrable_unique_constraint_matches_between_generate_schemas_and_migrate(db_simple):
    """The two independently-maintained DEFERRABLE-emission sites -
    BaseSchemaGenerator._get_meta_constraints_sqls() (the generate_schemas() quick-start path) and
    ConstraintSchemaEditorMixin.add_constraint() (the migrate path, exercised here through a real
    AddConstraint migration operation) - must produce the SAME real DEFERRABLE behavior for the
    same UniqueConstraint(deferrable=True, initially_deferred=True), not merely similar-looking
    DDL text: both must actually defer the uniqueness check to COMMIT."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("DEFERRABLE unique constraints are Postgres-only")

    from hare.transactions.transactions import Transactions

    class Ticket(Model):
        id = IntField(primary_key=True)
        room = CharField(max_length=50)
        day = CharField(max_length=50)

        class Meta:
            table = "test_deferrable_unique_gs"
            app = "models"
            constraints = [
                UniqueConstraint(
                    fields=("room", "day"),
                    name="uq_ticket_room_day",
                    deferrable=True,
                    initially_deferred=True,
                )
            ]

    tbl = q("test_deferrable_unique_gs", dialect)

    async def assert_defers_uniqueness_check_to_commit() -> None:
        await conn.execute_script(f"INSERT INTO {tbl} (id, room, day) VALUES (1, 'A', 'Mon')")
        async with Transactions.atomic("models") as tx_conn:
            await tx_conn.execute(f"INSERT INTO {tbl} (id, room, day) VALUES (2, 'A', 'Mon')")
            await tx_conn.execute(f"UPDATE {tbl} SET day = 'Tue' WHERE id = 2")
        rows = await conn.execute_dicts(f"SELECT id, room, day FROM {tbl} ORDER BY id")
        assert rows == [
            {"id": 1, "room": "A", "day": "Mon"},
            {"id": 2, "room": "A", "day": "Tue"},
        ]

    try:
        # Path 1: generate_schemas() - BaseSchemaGenerator._get_table_sql()/
        # _get_meta_constraints_sqls().
        generator = conn.dialect.schema_editor_class(conn)
        create_sql = generator._get_model_sql_data(Ticket).get_table_creation_sql()
        await conn.execute_script(create_sql)
        await assert_defers_uniqueness_check_to_commit()
        await conn.execute_script(f"DROP TABLE {tbl}")

        # Path 2: migrate - a real CreateModel + AddConstraint migration operation, executed
        # through ConstraintSchemaEditorMixin.add_constraint().
        editor = _get_schema_editor(conn)
        create_op = CreateModel(
            name="Ticket",
            fields=[
                ("id", IntField(primary_key=True)),
                ("room", CharField(max_length=50)),
                ("day", CharField(max_length=50)),
            ],
            options={"table": "test_deferrable_unique_gs"},
        )
        state = State(models={}, apps=StateApps())
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        add_op = AddConstraint(
            model_name="Ticket",
            constraint=UniqueConstraint(
                fields=("room", "day"),
                name="uq_ticket_room_day",
                deferrable=True,
                initially_deferred=True,
            ),
        )
        await add_op.run("models", state, dry_run=False, state_editor=editor)
        await assert_defers_uniqueness_check_to_commit()
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_add_check_constraint(db_simple):
    """AddConstraint with CheckConstraint enforces data validation on real DB."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)

    editor = _get_schema_editor(conn)

    from hare.exceptions import IntegrityError, OperationalError

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", IntField(null=False)),
        ],
        options={"table": "test_product_ck"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        # Add check constraint
        constraint = CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_product_price_positive")
        add_op = AddConstraint(model_name="Product", constraint=constraint)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_product_ck", dialect)

        # Valid insert
        await conn.execute_script(f"INSERT INTO {tbl} ({q('price', dialect)}) VALUES (100)")

        # Invalid insert should be rejected
        with pytest.raises((IntegrityError, OperationalError)):
            await conn.execute_script(f"INSERT INTO {tbl} ({q('price', dialect)}) VALUES (-5)")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product_ck', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_add_exclusion_constraint_with_raw_sql_expression_enforces_exclusion(db_simple):
    """ExclusionConstraint.expressions used to only accept a plain field name - a real Postgres
    EXCLUDE constraint's whole point is combining arbitrary expressions with operators, e.g.
    computing a range on the fly via ``tstzrange(start_date, end_date)`` rather than requiring a
    stored range column. A RawSQLTerm expression entry now lets that expression be spliced into
    the EXCLUDE clause verbatim, mixed with a plain field-name entry (``resource``)."""
    from hare.ddl.constraints import ExclusionConstraint
    from hare.ddl.raw_sql_term import RawSQLTerm
    from hare.exceptions import IntegrityError
    from hare.migrations.operations import AddConstraint

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("ExclusionConstraint/EXCLUDE is Postgres-only")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Order",
        fields=[
            ("id", IntField(primary_key=True)),
            ("resource", IntField()),
            ("start_date", DatetimeField()),
            ("end_date", DatetimeField()),
        ],
        options={"table": "test_order_excl"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await conn.execute_script("CREATE EXTENSION IF NOT EXISTS btree_gist;")
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        constraint = ExclusionConstraint(
            name="no_overlap_range",
            expressions=(
                ("resource", "="),
                (RawSQLTerm("tstzrange(start_date, end_date)"), "&&"),
            ),
        )
        add_op = AddConstraint(model_name="Order", constraint=constraint)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_order_excl", dialect)

        async def insert(resource: int, start: str, end: str) -> None:
            await conn.execute_script(
                f"INSERT INTO {tbl} (resource, start_date, end_date) VALUES ({resource}, '{start}', '{end}')"
            )

        # Baseline order for resource 1.
        await insert(1, "2026-01-01 10:00:00", "2026-01-01 12:00:00")

        # Overlapping range on the SAME resource must be rejected.
        with pytest.raises(IntegrityError):
            await insert(1, "2026-01-01 11:00:00", "2026-01-01 13:00:00")

        # A non-overlapping range on the same resource is fine.
        await insert(1, "2026-01-01 12:00:00", "2026-01-01 14:00:00")

        # An overlapping range on a DIFFERENT resource is fine (the "resource =" term scopes
        # the exclusion, proving the plain-field-name entry and the raw-expression entry are
        # both actually taking part in the constraint, not just the expression alone).
        await insert(2, "2026-01-01 10:30:00", "2026-01-01 11:30:00")

        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 3
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_order_excl', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_field_updates_a_rawsqlterm_inside_an_exclusion_constraint(db_simple):
    """RenameField's own Meta-reference sync updated a plain-field-name ExclusionConstraint.
    expressions entry (`field_name == self.old_name`) but never looked inside a RawSQLTerm
    entry's own raw SQL text - the sibling gap CheckConstraint.check/UniqueConstraint.condition/
    Trigger.body already had fixed. Confirmed live before this fix: the tracked RawSQLTerm kept
    the OLD column name forever, even though Postgres itself correctly rewrites the real DB-level
    constraint definition on RENAME COLUMN (tracked by internal column id, not name) - a
    squashed/regenerated migration built from that stale tracked text would then fail with
    "column start_date does not exist" against a fresh database."""
    from hare.ddl.constraints import ExclusionConstraint
    from hare.ddl.raw_sql_term import RawSQLTerm

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("ExclusionConstraint/EXCLUDE is Postgres-only")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Order",
        fields=[
            ("id", IntField(primary_key=True)),
            ("resource", IntField()),
            ("start_date", DatetimeField()),
            ("end_date", DatetimeField()),
        ],
        options={
            "table": "test_order_excl_rename",
            "constraints": [
                ExclusionConstraint(
                    name="no_overlap_range_rename",
                    expressions=(
                        ("resource", "="),
                        (RawSQLTerm("tstzrange(start_date, end_date)"), "&&"),
                    ),
                )
            ],
        },
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_order_excl_rename"

    try:
        await conn.execute_script("CREATE EXTENSION IF NOT EXISTS btree_gist;")
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        rename_op = RenameField(model_name="Order", old_name="start_date", new_name="begin_date")
        await rename_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Order")]
        (constraint,) = model_state.get_option_list("constraints")
        raw_term = constraint.expressions[1][0]
        assert isinstance(raw_term, RawSQLTerm)
        assert raw_term.sql == "tstzrange(begin_date, end_date)"
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_rejects_a_rawsqlterm_reference_inside_an_exclusion_constraint(db_simple):
    """RemoveField's own Meta-reference cleanup checked a plain-field-name ExclusionConstraint.
    expressions entry but never looked inside a RawSQLTerm entry's own raw SQL text - a field
    named ONLY inside the RawSQLTerm (not as a separate plain-field expressions entry) sailed
    through unblocked. Confirmed live before this fix: `ALTER TABLE ... DROP COLUMN end_date`
    silently dropped the WHOLE EXCLUDE constraint too (its GiST index depended on the column),
    with no error and no CASCADE - the range-overlap protection vanished permanently while the
    tracked state kept believing the constraint still existed."""
    from hare.ddl.constraints import ExclusionConstraint
    from hare.ddl.raw_sql_term import RawSQLTerm

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("ExclusionConstraint/EXCLUDE is Postgres-only")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Order",
        fields=[
            ("id", IntField(primary_key=True)),
            ("resource", IntField()),
            ("start_date", DatetimeField()),
            ("end_date", DatetimeField()),
        ],
        options={
            "table": "test_order_excl_removefield",
            "constraints": [
                ExclusionConstraint(
                    name="no_overlap_range_removefield",
                    expressions=(
                        ("resource", "="),
                        (RawSQLTerm("tstzrange(start_date, end_date)"), "&&"),
                    ),
                )
            ],
        },
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_order_excl_removefield"

    try:
        await conn.execute_script("CREATE EXTENSION IF NOT EXISTS btree_gist;")
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        remove_op = RemoveField(model_name="Order", name="end_date")
        with pytest.raises(ConfigurationError, match="still referenced by constraint"):
            await remove_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Order")]
        assert "end_date" in model_state.fields
        assert len(model_state.get_option_list("constraints")) == 1
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_add_partial_index_with_raw_sql_non_equality_predicate_applies_selectively(db_simple):
    """A raw SQL condition (RawSQLTerm) is the index's WHERE clause as written - a predicate like
    "price > 100 OR is_featured" (a non-equality comparison ORed with another column)."""
    from hare.ddl.indexes import PartialIndex
    from hare.migrations.operations import AddIndex

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("PartialIndex is Postgres-only")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", IntField()),
            ("is_featured", BooleanField()),
        ],
        options={"table": "test_product_partial_idx"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        index = PartialIndex(
            fields=("price",), name="idx_product_promo", condition=RawSQLTerm("price > 100 OR is_featured")
        )
        add_op = AddIndex(model_name="Product", index=index)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_product_partial_idx", dialect)

        # 3 rows match the predicate (price > 100 or featured), 7 don't.
        matching = [(150, False), (50, True), (200, True)]
        non_matching = [(10, False), (20, False), (30, False), (40, False), (50, False), (60, False), (100, False)]
        for price, is_featured in matching + non_matching:
            await conn.execute_script(
                f"INSERT INTO {tbl} (price, is_featured) VALUES ({price}, {str(is_featured).upper()})"
            )

        indexdef_rows = await conn.execute_dicts(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_product_promo'"
        )
        assert len(indexdef_rows) == 1
        indexdef = indexdef_rows[0]["indexdef"]
        assert "WHERE" in indexdef
        assert "price" in indexdef and "is_featured" in indexdef

        await conn.execute_script(f"ANALYZE {tbl};")
        reltuples_rows = await conn.execute_dicts("SELECT reltuples FROM pg_class WHERE relname = 'idx_product_promo'")
        assert len(reltuples_rows) == 1
        # The partial index only covers the 3 matching rows, not all 10 - proof Postgres is
        # actually applying the predicate selectively, not just accepting the DDL without error.
        assert reltuples_rows[0]["reltuples"] == pytest.approx(len(matching))
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product_partial_idx', dialect)}")
        except Exception:
            pass


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_sqlite_remove_unique_constraint_declared_in_first_migration(db_simple):
    """An unnamed UniqueConstraint present from the model's very FIRST migration is a unique
    index of its generated name on SQLite - never an inline UNIQUE, whose backing index SQLite
    names sqlite_autoindex_<table>_<N> and won't drop. A later RemoveConstraint for it drops
    the real index."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("sqlite_autoindex renaming is SQLite-specific")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Employee",
        fields=[
            ("id", IntField(primary_key=True)),
            ("email", CharField(max_length=200)),
        ],
        options={"table": "test_employee_ut_first", "constraints": [UniqueConstraint(fields=("email",))]},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_employee_ut_first", dialect)
        rows = await conn.execute_dicts(f"PRAGMA index_list({tbl})")
        assert len(rows) == 1
        assert not rows[0]["name"].startswith("sqlite_autoindex_")

        remove_op = RemoveConstraint(model_name="Employee", fields=["email"])
        await remove_op.run("models", state, dry_run=False, state_editor=editor)

        rows_after = await conn.execute_dicts(f"PRAGMA index_list({tbl})")
        assert rows_after == []

        await conn.execute_script(f"INSERT INTO {tbl} ({q('email', dialect)}) VALUES ('a@test.com')")
        await conn.execute_script(f"INSERT INTO {tbl} ({q('email', dialect)}) VALUES ('a@test.com')")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_employee_ut_first', dialect)}")
        except Exception:
            pass


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_remove_unnamed_uniqueconstraint_declared_via_meta_constraints(db_simple):
    """A UniqueConstraint declared via Meta.constraints (not unique_together) can have no
    explicit name= at all - unlike CheckConstraint/ExclusionConstraint, which always require
    one. RemoveConstraint._get_constraint()'s fields= fallback used to only search
    unique_together, never Meta.constraints, so it couldn't find (and therefore couldn't drop)
    an unnamed UniqueConstraint declared this way at all."""
    from hare.exceptions import IntegrityError

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Order",
        fields=[
            ("id", IntField(primary_key=True)),
            ("resource", IntField()),
            ("slug", CharField(max_length=50)),
        ],
        options={
            "table": "test_order_unnamed_uc",
            "constraints": [UniqueConstraint(fields=("resource", "slug"))],
        },
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_order_unnamed_uc", dialect)
        resource_col = q("resource", dialect)
        slug_col = q("slug", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} ({resource_col}, {slug_col}) VALUES (1, 'a')")
        with pytest.raises(IntegrityError):
            await conn.execute_script(f"INSERT INTO {tbl} ({resource_col}, {slug_col}) VALUES (1, 'a')")

        remove_op = RemoveConstraint(model_name="Order", fields=["resource", "slug"])
        await remove_op.run("models", state, dry_run=False, state_editor=editor)

        # The constraint is gone - the same insert that raised above now succeeds.
        await conn.execute_script(f"INSERT INTO {tbl} ({resource_col}, {slug_col}) VALUES (1, 'a')")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 2
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_order_unnamed_uc', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_remove_check_constraint_actually_drops_it(db_simple):
    """RemoveConstraint for a CheckConstraint used to be a silent no-op on SQLite - it rebuilt
    the table via _remake_table() with no way to tell the rebuild which constraint to exclude, so
    every CHECK constraint in Meta.constraints (including the one being removed) got faithfully
    re-emitted into the new table."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("_remake_table is SQLite-specific")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", IntField(null=False)),
        ],
        options={"table": "test_product_ck_remove"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        constraint = CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_product_price_positive_remove")
        add_op = AddConstraint(model_name="Product", constraint=constraint)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        remove_op = RemoveConstraint(model_name="Product", name="ck_product_price_positive_remove")
        await remove_op.run("models", state, dry_run=False, state_editor=editor)

        sql_row = await conn.execute_dicts("SELECT sql FROM sqlite_master WHERE name = 'test_product_ck_remove'")
        assert "ck_product_price_positive_remove" not in sql_row[0]["sql"]

        tbl = q("test_product_ck_remove", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('price', dialect)}) VALUES (-5)")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product_ck_remove', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_field_null_change(db_simple):
    """AlterField can change a column from NOT NULL to NULL and back."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)

    if dialect == "sqlite":
        pytest.skip("SQLite does not support ALTER COLUMN nullability changes")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Config",
        fields=[
            ("id", IntField(primary_key=True)),
            ("value", IntField(null=False, default=0)),
        ],
        options={"table": "test_config"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_config", dialect)

        # Currently NOT NULL — inserting NULL should fail
        with pytest.raises(Exception):
            await conn.execute_script(f"INSERT INTO {tbl} ({q('value', dialect)}) VALUES (NULL)")

        # AlterField: make nullable
        alter_op = AlterField(
            model_name="Config",
            name="value",
            field=IntField(null=True),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        # Now NULL should be accepted
        await conn.execute_script(f"INSERT INTO {tbl} ({q('value', dialect)}) VALUES (NULL)")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        # Find the row with NULL value
        null_rows = [r for r in rows if r["value"] is None]
        assert len(null_rows) == 1

        # AlterField: make NOT NULL again
        # First, update the NULL row so ALTER doesn't fail
        await conn.execute_script(f"UPDATE {tbl} SET {q('value', dialect)} = 0 WHERE {q('value', dialect)} IS NULL")

        alter_back_op = AlterField(
            model_name="Config",
            name="value",
            field=IntField(null=False),
        )
        await alter_back_op.run("models", state, dry_run=False, state_editor=editor)

        # NULL should be rejected again
        with pytest.raises(Exception):
            await conn.execute_script(f"INSERT INTO {tbl} ({q('value', dialect)}) VALUES (NULL)")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_config', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_field_rename_combined_with_type_and_null_change(db_simple):
    """_alter_field() used to defer the RENAME COLUMN statement to run LAST in its combined
    multi-statement SQL, while every other branch (type, NOT NULL, default) already referenced
    the NEW column name - on Postgres (the only dialect that shares this base _alter_field(); SQLite
    has its own separate override), combining a rename with any of those other changes in one
    AlterField crashed with "column ... does not exist", since the rename hadn't happened yet
    when the earlier statements in the batch ran."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect == "sqlite":
        pytest.skip("SQLite's _alter_field() is a separate override, not the code path under test")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("old_col", CharField(max_length=50, null=True)),
        ],
        options={"table": "test_widget_alter_rename"},
    )
    state = State(models={}, apps=StateApps())
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        alter_op = AlterField(
            model_name="Widget",
            name="old_col",
            field=CharField(max_length=50, null=False, source_field="new_col", db_default="x"),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_widget_alter_rename", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} DEFAULT VALUES")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert rows[0]["new_col"] == "x"
        with pytest.raises(Exception):
            await conn.execute_script(f"INSERT INTO {tbl} ({q('new_col', dialect)}) VALUES (NULL)")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_widget_alter_rename', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_field_rename_combined_with_nullable_change(db_simple):
    """Sibling of the test above for the OTHER null-change direction (NOT NULL -> nullable) -
    that specific branch used old_db_field (not new_db_field), which was only "accidentally"
    correct under the OLD (rename-last) ordering; fixing the rename to run first without also
    fixing this branch would have broken it in the other direction."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect == "sqlite":
        pytest.skip("SQLite's _alter_field() is a separate override, not the code path under test")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("old_col", CharField(max_length=50, null=False, default="x")),
        ],
        options={"table": "test_widget_alter_rename_nullable"},
    )
    state = State(models={}, apps=StateApps())
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        alter_op = AlterField(
            model_name="Widget",
            name="old_col",
            field=CharField(max_length=50, null=True, source_field="new_col"),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_widget_alter_rename_nullable", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('new_col', dialect)}) VALUES (NULL)")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert rows[0]["new_col"] is None
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_widget_alter_rename_nullable', dialect)}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Issue #2141 reproduction tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alter_field_max_length_preserves_db_default(db_simple):
    """Changing max_length on a field with db_default must not lose the default.

    Reproduces https://github.com/hare/hare-orm/issues/2141 (bug 1).

    On MySQL, MODIFY COLUMN resets the full column definition.  If the
    migration editor only emits ``MODIFY COLUMN col VARCHAR(20) NOT NULL``
    without re-applying the DEFAULT, the database default silently disappears.
    """
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)

    if dialect == "sqlite":
        pytest.skip("SQLite uses table-recreation, not ALTER COLUMN")

    editor = _get_schema_editor(conn)

    # Create table with a VARCHAR(10) column that has db_default=''
    create_op = CreateModel(
        name="Profile",
        fields=[
            ("id", IntField(primary_key=True)),
            ("tag", CharField(max_length=50)),
            ("name", CharField(max_length=10, db_default="")),
        ],
        options={"table": "test_profile_2141"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_profile_2141", dialect)

        # Verify the default works before any ALTER
        await conn.execute_script(f"INSERT INTO {tbl} ({q('tag', dialect)}) VALUES ('before_alter')")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl} WHERE {q('tag', dialect)} = 'before_alter'")
        assert rows[0]["name"] == "", "db_default='' should produce empty string"

        # AlterField: change max_length from 10 -> 20, keeping db_default=''
        alter_op = AlterField(
            model_name="Profile",
            name="name",
            field=CharField(max_length=20, db_default=""),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        # Insert again omitting 'name' — the default should still work
        await conn.execute_script(f"INSERT INTO {tbl} ({q('tag', dialect)}) VALUES ('after_alter')")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl} WHERE {q('tag', dialect)} = 'after_alter'")
        assert rows[0]["name"] == "", "db_default='' was lost after AlterField changed max_length"
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_profile_2141', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_field_null_change_preserves_db_default(db_simple):
    """Changing nullability on a field with db_default must not lose the default.

    Reproduces a variant of https://github.com/hare/hare-orm/issues/2141
    (bug 1) where the trigger is a nullability change instead of max_length.
    """
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)

    if dialect == "sqlite":
        pytest.skip("SQLite uses table-recreation, not ALTER COLUMN")

    editor = _get_schema_editor(conn)

    # Create table with db_default=99 on an integer column
    create_op = CreateModel(
        name="Score",
        fields=[
            ("id", IntField(primary_key=True)),
            ("tag", CharField(max_length=50)),
            ("value", IntField(null=False, db_default=99)),
        ],
        options={"table": "test_score_2141"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_score_2141", dialect)

        # Verify the default works before any ALTER
        await conn.execute_script(f"INSERT INTO {tbl} ({q('tag', dialect)}) VALUES ('before')")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl} WHERE {q('tag', dialect)} = 'before'")
        assert rows[0]["value"] == 99

        # AlterField: make nullable, keep same db_default
        alter_op = AlterField(
            model_name="Score",
            name="value",
            field=IntField(null=True, db_default=99),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        # The default should still work after the null change
        await conn.execute_script(f"INSERT INTO {tbl} ({q('tag', dialect)}) VALUES ('after_null')")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl} WHERE {q('tag', dialect)} = 'after_null'")
        assert rows[0]["value"] == 99, "db_default=99 was lost after AlterField changed nullability"
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_score_2141', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_field_description_change_applied(db_simple):
    """Changing only a field's description should execute actual DDL.

    Reproduces https://github.com/hare/hare-orm/issues/2141 (bug 2).

    The migration diff detects description changes (migration file IS created),
    but ``_alter_field`` has ``pass`` for description changes, so no SQL runs.
    On PostgreSQL this should emit ``COMMENT ON COLUMN``.
    """
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)

    if dialect == "sqlite":
        pytest.skip("SQLite does not support column comments")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Item",
        fields=[
            ("id", IntField(primary_key=True)),
            ("name", CharField(max_length=100, description="item name")),
        ],
        options={"table": "test_item_2141"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        # AlterField: change only the description
        alter_op = AlterField(
            model_name="Item",
            name="name",
            field=CharField(max_length=100, description="short item name"),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        # Verify the comment was actually updated in the database
        tbl = "test_item_2141"
        if dialect in ("postgresql",):
            query = (
                "SELECT col_description(c.oid, a.attnum) AS comment "
                "FROM pg_class c "
                "JOIN pg_attribute a ON a.attrelid = c.oid "
                f"WHERE c.relname = '{tbl}' AND a.attname = 'name'"
            )
            rows = await conn.execute_dicts(query)
            comment = rows[0]["comment"] if rows else None
            assert comment == "short item name", f"PostgreSQL column comment not updated: got {comment!r}"
        else:
            pytest.skip(f"Comment introspection not implemented for {dialect}")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_item_2141', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_field_class_change_preserves_data(db_simple):
    """AlterField changing a field's Python-level class (IntField -> DecimalField, not just its
    SQL_TYPE within the same class) used to hard-block with "Automatic field type altering is
    not supported yet ... Please use AlterFieldManual" - a class that doesn't exist anywhere in
    the codebase - before ever reaching either dialect's real handling: SQLite's _alter_field()
    already rebuilds the whole table for any alteration, and Postgres's ALTER COLUMN TYPE
    already works for this exact cast pair without needing a USING clause. Both must now apply
    the type change and preserve existing data."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Invoice",
        fields=[
            ("id", IntField(primary_key=True)),
            ("amount", IntField()),
        ],
        options={"table": "test_invoice_class_change"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_invoice_class_change", dialect)

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('amount', dialect)}) VALUES (42)")

        alter_op = AlterField(
            model_name="Invoice",
            name="amount",
            field=DecimalField(max_digits=10, decimal_places=2),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 1
        assert float(rows[0]["amount"]) == 42.0
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_autodetect_and_apply_pk_field_rename(db_simple):
    """The autodetector must recognize an unambiguous PK-field rename and emit RenameField,
    not raise "changing the primary key ... is not supported" - that guard used to fire on ANY
    pk_field_name change before the rename heuristic (which already handles the DB-level rename
    correctly, including pk_field_name/pk_attr bookkeeping) ever got a chance to run. Builds the
    old/new ModelStates directly from live model classes (as a real makemigrations run would),
    generates operations from scratch, and applies the resulting RenameField against a real
    table."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    tbl = q("test_invoice_pk_rename", dialect)

    # Both revisions of the model are literally named "Invoice" (via type(), not a plain class
    # statement) - a rename detects as the SAME model evolving, keyed the same way makemigrations
    # itself keys state.models (by class __name__), unlike two differently-named classes which
    # would just look like an unrelated old/new model pair.
    OldInvoice = type(
        "Invoice",
        (Model,),
        {
            "id": IntField(primary_key=True),
            "title": CharField(max_length=50),
            "Meta": type("Meta", (), {"table": "test_invoice_pk_rename", "app": "models"}),
            "_no_comments": True,
        },
    )
    NewInvoice = type(
        "Invoice",
        (Model,),
        {
            "invoice_id": IntField(primary_key=True, source_field="id"),
            "title": CharField(max_length=50),
            "Meta": type("Meta", (), {"table": "test_invoice_pk_rename", "app": "models"}),
            "_no_comments": True,
        },
    )

    old_state = State(models={}, apps=StateApps())
    old_state.models[("models", "Invoice")] = ModelState.make_from_model("models", OldInvoice)
    new_state = State(models={}, apps=StateApps())
    new_state.models[("models", "Invoice")] = ModelState.make_from_model("models", NewInvoice)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], RenameField)
    assert operations[0].old_name == "id"
    assert operations[0].new_name == "invoice_id"

    apply_state = State(models={}, apps=StateApps())
    try:
        await CreateModel(
            name="Invoice",
            fields=[("id", IntField(primary_key=True)), ("title", CharField(max_length=50))],
            options={"table": "test_invoice_pk_rename"},
        ).run("models", apply_state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('id', dialect)}, {q('title', dialect)}) VALUES (1, 'x')")

        for operation in operations:
            await operation.run("models", apply_state, dry_run=False, state_editor=editor)

        # The generated RenameField carries source_field="id" (the disambiguation marker the
        # autodetector's rename heuristic itself requires - see field_diff.py's "an intentional
        # rename can always be spelled out this way" comment), so the real DB column deliberately
        # keeps its old name "id" here; only the Python-level field name became "invoice_id". The
        # column still works as the real PRIMARY KEY either way - a duplicate value is rejected.
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 1
        assert rows[0]["id"] == 1
        assert rows[0]["title"] == "x"

        with pytest.raises(Exception):
            await conn.execute_script(
                f"INSERT INTO {tbl} ({q('id', dialect)}, {q('title', dialect)}) VALUES (1, 'dup')"
            )

        projected_model_state = apply_state.models[("models", "Invoice")]
        assert projected_model_state.pk_field_name == "invoice_id"
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_foreign_key(db_isolated):
    """remove_field() on a plain (non-unique) FK column must succeed without an explicit
    constraint-drop step - both dialects already handle this: SQLite via full-table rebuild (the
    column and everything tied to it just isn't in the new table), and Postgres because DROP
    COLUMN automatically drops the FK constraint it owns, no CASCADE required."""
    from tests.testmodels import Author, Book

    conn = db_isolated.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    author_tbl = q(Author._meta.db_table, dialect)
    book_tbl = q(Book._meta.db_table, dialect)
    # Postgres needs CASCADE here: db_isolated's own generate_schemas() already created every
    # OTHER testmodels table too, including some with their own FK pointing at "author" (e.g.
    # O2oPkModelWithM2m, VersionedDocumentWithAuthor) - harmless to cascade away in THIS test's
    # own completely fresh, per-test database. SQLite has no CASCADE keyword on DROP TABLE at all.
    drop_suffix = " CASCADE" if dialect != "sqlite" else ""

    await conn.execute_script(f"DROP TABLE IF EXISTS {book_tbl}{drop_suffix}")
    await conn.execute_script(f"DROP TABLE IF EXISTS {author_tbl}{drop_suffix}")
    try:
        await editor.create_model(Author)
        await editor.create_model(Book)

        await conn.execute_script(f'INSERT INTO {author_tbl} ("id", "name") VALUES (1, \'A\')')
        await conn.execute_script(
            f'INSERT INTO {book_tbl} ("id", "name", "rating", "author_id") VALUES (1, \'T\', 4.5, 1)'
        )

        author_field = Book._meta.fields_map["author"]
        await editor.remove_field(Book, author_field)

        rows = await conn.execute_dicts(f"SELECT * FROM {book_tbl}")
        assert len(rows) == 1
        assert "author_id" not in rows[0]
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {book_tbl}{drop_suffix}")
            await conn.execute_script(f"DROP TABLE IF EXISTS {author_tbl}{drop_suffix}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_foreign_key_with_own_unique_constraint(db_isolated):
    """remove_field() on an FK column that also carries its own UNIQUE constraint (a
    OneToOneField) must succeed without an explicit constraint-drop step - regression guard for
    the concern the remove_field() TODO used to flag (dropping an FK column can fail if the FK
    constraint itself, or a dependent index/unique constraint, blocks DROP COLUMN). Verified
    against real Postgres: DROP COLUMN already drops any constraint/index owned solely by that
    column (FK, UNIQUE, CHECK, plain index) automatically, no CASCADE required - SQLite sidesteps
    the question entirely via full-table rebuild."""
    from tests.testmodels import SoftDeleteChildProtectO2O, SoftDeleteParent

    conn = db_isolated.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    parent_tbl = q(SoftDeleteParent._meta.db_table, dialect)
    child_tbl = q(SoftDeleteChildProtectO2O._meta.db_table, dialect)
    # See test_remove_field_foreign_key's identical comment: SoftDeleteParent is referenced by
    # several OTHER testmodels tables db_isolated's own schema setup already created.
    drop_suffix = " CASCADE" if dialect != "sqlite" else ""

    await conn.execute_script(f"DROP TABLE IF EXISTS {child_tbl}{drop_suffix}")
    await conn.execute_script(f"DROP TABLE IF EXISTS {parent_tbl}{drop_suffix}")
    try:
        await editor.create_model(SoftDeleteParent)
        await editor.create_model(SoftDeleteChildProtectO2O)

        await conn.execute_script(f'INSERT INTO {parent_tbl} ("id", "name") VALUES (1, \'P\')')
        await conn.execute_script(f'INSERT INTO {child_tbl} ("id", "name", "parent_id") VALUES (1, \'C\', 1)')

        parent_field = SoftDeleteChildProtectO2O._meta.fields_map["parent"]
        await editor.remove_field(SoftDeleteChildProtectO2O, parent_field)

        rows = await conn.execute_dicts(f"SELECT * FROM {child_tbl}")
        assert len(rows) == 1
        assert "parent_id" not in rows[0]
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {child_tbl}{drop_suffix}")
            await conn.execute_script(f"DROP TABLE IF EXISTS {parent_tbl}{drop_suffix}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_remake_table_coalesces_null_on_tightening(db_simple):
    """SQLite's _remake_table: nullable->non-null with a default COALESCEs existing NULLs
    instead of failing the rebuild (only exercised by SQLite - other dialects use a direct
    ALTER COLUMN and are covered by test_alter_field_null_change instead)."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("_remake_table is SQLite-specific")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("stock", IntField(null=True)),
        ],
        options={"table": "test_widget_coalesce"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        tbl = q("test_widget_coalesce", dialect)

        await conn.execute_script(f"INSERT INTO {tbl} ({q('stock', dialect)}) VALUES (NULL)")
        await conn.execute_script(f"INSERT INTO {tbl} ({q('stock', dialect)}) VALUES (7)")

        alter_op = AlterField(
            model_name="Widget",
            name="stock",
            field=IntField(null=False, default=0),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('stock', dialect)} FROM {tbl} ORDER BY 1")
        assert sorted(r["stock"] for r in rows) == [0, 7]
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_widget_coalesce', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_remake_table_coalesces_null_with_quote_in_string_default(db_simple):
    """_remake_table's null->non-null backfill must correctly escape a string default
    containing a single quote - the old hand-rolled f"'{value}'" produced malformed SQL for
    exactly this case; _backfill_default_sql_literal() now reuses the same
    to_db_value()+_escape_default_value() pipeline already used for db_default."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("_remake_table is SQLite-specific")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("note", CharField(max_length=50, null=True)),
        ],
        options={"table": "test_widget_quote_default"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        tbl = q("test_widget_quote_default", dialect)

        await conn.execute_script(f"INSERT INTO {tbl} ({q('note', dialect)}) VALUES (NULL)")

        alter_op = AlterField(
            model_name="Widget",
            name="note",
            field=CharField(max_length=50, null=False, default="it's fine"),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('note', dialect)} FROM {tbl}")
        assert rows[0]["note"] == "it's fine"
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_widget_quote_default', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_remake_table_coalesces_null_with_db_default(db_simple):
    """_remake_table's null->non-null backfill only ever checked new_field.default - a field
    tightened to NOT NULL via db_default= instead (the idiomatic way to add a required column
    with a default, mutually exclusive with default=) had its existing NULL rows copied as-is
    into the rebuilt table, crashing the whole migration with a NOT NULL constraint violation
    instead of backfilling, even though this is exactly the scenario the surrounding COALESCE
    logic was written for."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("_remake_table is SQLite-specific")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("note", CharField(max_length=50, null=True)),
        ],
        options={"table": "test_widget_db_default"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        tbl = q("test_widget_db_default", dialect)

        await conn.execute_script(f"INSERT INTO {tbl} ({q('note', dialect)}) VALUES (NULL)")

        alter_op = AlterField(
            model_name="Widget",
            name="note",
            field=CharField(max_length=50, null=False, db_default="n/a"),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('note', dialect)} FROM {tbl}")
        assert rows[0]["note"] == "n/a"
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_widget_db_default', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_remake_table_rejects_callable_default_on_tightening(db_simple):
    """A callable Python-level default (e.g. default=list/default=uuid4, both explicitly
    allowed by Field.__init__) can't be safely backfilled by _remake_table - computing it once
    would give every existing row the same value, wrong for something like uuid4. Must raise a
    clear ConfigurationError instead of stringifying the function repr into broken SQL."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("_remake_table is SQLite-specific")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("note", CharField(max_length=50, null=True)),
        ],
        options={"table": "test_widget_callable_default"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        alter_op = AlterField(
            model_name="Widget",
            name="note",
            field=CharField(max_length=50, null=False, default=lambda: "generated"),
        )
        with pytest.raises(ConfigurationError, match="callable"):
            await alter_op.run("models", state, dry_run=False, state_editor=editor)
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_widget_callable_default', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_remake_table_preserves_check_constraint(db_simple):
    """SQLite's _remake_table: an unrelated AlterField must not silently drop existing CHECK
    constraints when the table is rebuilt."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("_remake_table is SQLite-specific")

    editor = _get_schema_editor(conn)

    from hare.exceptions import IntegrityError, OperationalError

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", IntField(null=False)),
            ("label", CharField(max_length=50, null=True)),
        ],
        options={"table": "test_product_remake_ck"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        constraint = CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_remake_price_positive")
        add_op = AddConstraint(model_name="Product", constraint=constraint)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        # Unrelated field alter triggers a full table remake.
        alter_op = AlterField(
            model_name="Product",
            name="label",
            field=CharField(max_length=100, null=True),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_product_remake_ck", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('price', dialect)}) VALUES (100)")
        with pytest.raises((IntegrityError, OperationalError)):
            await conn.execute_script(f"INSERT INTO {tbl} ({q('price', dialect)}) VALUES (-5)")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product_remake_ck', dialect)}")
        except Exception:
            pass


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_sqlite_remake_table_preserves_indexes_and_unique_together(db_simple):
    """SQLite's _remake_table: an unrelated AlterField must not silently drop Meta.indexes or
    Meta.unique_together when the table is rebuilt - the same class of bug already fixed once for
    Meta.constraints's own UniqueConstraint entries (see test_sqlite_remake_table_preserves_
    check_constraint above), but unique_together is a distinct _meta attribute and Meta.indexes
    was never covered by that earlier fix at all."""
    from hare.ddl.indexes import Index
    from hare.exceptions import IntegrityError, OperationalError

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("_remake_table is SQLite-specific")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("sku", CharField(max_length=50)),
            ("label", CharField(max_length=50, null=True)),
        ],
        options={
            "table": "test_product_remake_idx",
            "indexes": [Index(fields=("sku",))],
            "constraints": [UniqueConstraint(fields=("sku", "label"))],
        },
    )
    state = State(models={}, apps=StateApps())

    async def index_names() -> set[str]:
        rows = await conn.execute_dicts(f"PRAGMA index_list({q('test_product_remake_idx', dialect)})")
        return {row["name"] for row in rows}

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        indexes_before = await index_names()
        assert len(indexes_before) == 2  # Meta.indexes entry + unique_together's own auto-index

        # Unrelated field alter triggers a full table remake.
        alter_op = AlterField(
            model_name="Product",
            name="label",
            field=CharField(max_length=100, null=True),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        assert await index_names() == indexes_before

        tbl = q("test_product_remake_idx", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} (\"sku\", \"label\") VALUES ('A', 'B')")
        with pytest.raises((IntegrityError, OperationalError)):
            await conn.execute_script(f"INSERT INTO {tbl} (\"sku\", \"label\") VALUES ('A', 'B')")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product_remake_idx', dialect)}")
        except Exception:
            pass


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_sqlite_create_model_with_check_and_unique_constraint_does_not_double_apply_unique(db_simple):
    """A model with BOTH a CheckConstraint and a UniqueConstraint in Meta.constraints used to get
    its unique constraint created TWICE on SQLite at CreateModel time: the outer create_model()
    loop calls add_constraint() once per Meta.constraints entry, and add_constraint() for the
    CheckConstraint triggers a full _remake_table() whose field-definition builder already
    re-embeds EVERY entry of model._meta.constraints (Check AND Unique alike) inline into the
    rebuilt CREATE TABLE - the loop then reached the UniqueConstraint entry and called
    add_constraint() on it too, issuing a SECOND, separately-named CREATE UNIQUE INDEX for a
    constraint already inline (confirmed via PRAGMA index_list: an inline autoindex from the
    rebuild PLUS an explicit uid_ index for the same columns)."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("this double-apply only happens through SQLite's remake-table path")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", IntField(null=False)),
            ("sku", CharField(max_length=50)),
        ],
        options={
            "table": "test_product_ck_uid_double",
            "constraints": [
                CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_product_price_positive_dbl"),
                UniqueConstraint(fields=("sku",), name="uid_product_sku_dbl"),
            ],
        },
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_product_ck_uid_double", dialect)
        rows = await conn.execute_dicts(f"PRAGMA index_list({tbl})")
        unique_indexes = [row for row in rows if row["unique"]]
        assert len(unique_indexes) == 1

        await conn.execute_script(f"INSERT INTO {tbl} ({q('price', dialect)}, {q('sku', dialect)}) VALUES (1, 'A')")
        with pytest.raises(Exception):
            await conn.execute_script(
                f"INSERT INTO {tbl} ({q('price', dialect)}, {q('sku', dialect)}) VALUES (2, 'A')"
            )
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product_ck_uid_double', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_create_model_with_generated_field_and_check_constraint_keeps_generated_column(db_simple):
    """CreateModel for a model with a non-PK GeneratedField AND a CheckConstraint in
    Meta.constraints used to silently lose the GENERATED ALWAYS AS (...) clause on SQLite: the
    CheckConstraint forces _add_meta_constraints() to rebuild the just-created table via
    _remake_table(), whose _build_remake_field_definitions() only special-cased a PK's own
    GeneratedField - a non-PK one fell into the generic column-definition branch and came back as
    an ordinary column, so an INSERT omitting it failed NOT NULL instead of the DB computing it."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("_remake_table is SQLite-specific")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", DecimalField(max_digits=10, decimal_places=2)),
            ("quantity", IntField()),
            (
                "total_value",
                GeneratedField(
                    expression="price * quantity",
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                ),
            ),
        ],
        options={
            "table": "test_product_generated_ck",
            "constraints": [
                CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_product_generated_price_positive"),
            ],
        },
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_product_generated_ck", dialect)
        sql_row = await conn.execute_dicts(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'test_product_generated_ck'"
        )
        assert "GENERATED ALWAYS AS" in sql_row[0]["sql"], sql_row[0]["sql"]

        await conn.execute_script(
            f"INSERT INTO {tbl} ({q('price', dialect)}, {q('quantity', dialect)}) VALUES (10, 3)"
        )
        result = await conn.execute_dicts(f"SELECT {q('total_value', dialect)} AS total FROM {tbl}")
        assert float(result[0]["total"]) == 30.0
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product_generated_ck', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_add_field_for_a_generated_field_keeps_generated_column(db_simple):
    """AddField for a non-PK GeneratedField outside CreateModel (so no _remake_table rebuild is
    forced - no CheckConstraint/composite FK involved) used to silently lose the GENERATED ALWAYS
    AS (...) clause on SQLite too: SqliteSchemaEditor.add_field()'s own override never checked
    field.generated at all, unlike FieldSchemaEditorMixin.add_field()'s base implementation - an
    ordinary writable column came back instead, so an INSERT omitting it failed NOT NULL instead
    of the DB computing it. This is exactly the path StateFieldDiff.generate_operations() takes
    for an EDITED GeneratedField expression too (RemoveField + AddField, not CreateModel)."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("sqlite-specific add_field() override under test")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", DecimalField(max_digits=10, decimal_places=2)),
            ("quantity", IntField()),
        ],
        options={"table": "test_product_addfield_generated"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_product_addfield_generated", dialect)

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        add_op = AddField(
            model_name="Product",
            name="total_value",
            field=GeneratedField(
                expression="price * quantity",
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        sql_row = await conn.execute_dicts(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'test_product_addfield_generated'"
        )
        assert "GENERATED ALWAYS AS" in sql_row[0]["sql"], sql_row[0]["sql"]

        await conn.execute_script(
            f"INSERT INTO {tbl} ({q('price', dialect)}, {q('quantity', dialect)}) VALUES (10, 3)"
        )
        result = await conn.execute_dicts(f"SELECT {q('total_value', dialect)} AS total FROM {tbl}")
        assert float(result[0]["total"]) == 30.0
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product_addfield_generated', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_add_field_for_a_generated_field_on_a_non_empty_table(db_simple):
    """SQLite only permits `ALTER TABLE ADD COLUMN ... GENERATED ALWAYS AS (...) STORED` on an
    EMPTY table - the sibling test above never hits this because it adds the column before
    inserting any rows. SqliteSchemaEditor.add_field() used to always take that direct ALTER
    TABLE path for a new non-pk GeneratedField regardless of the table's row count, crashing
    with "cannot add a STORED column" the moment the table already held data - a completely
    realistic case (adding a computed column to an existing, populated table). Confirmed live
    before this fix. Fixed by routing through _remake_table() (a full rebuild, which works
    regardless of row count) instead."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("sqlite-specific add_field() row-count limit under test")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", DecimalField(max_digits=10, decimal_places=2)),
            ("quantity", IntField()),
        ],
        options={"table": "test_product_addfield_generated_nonempty"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_product_addfield_generated_nonempty", dialect)

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(
            f"INSERT INTO {tbl} ({q('price', dialect)}, {q('quantity', dialect)}) VALUES (10, 3), (7, 5)"
        )

        add_op = AddField(
            model_name="Product",
            name="total_value",
            field=GeneratedField(
                expression="price * quantity",
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        result = await conn.execute_dicts(
            f"SELECT {q('total_value', dialect)} AS total FROM {tbl} ORDER BY {q('id', dialect)}"
        )
        assert [float(row["total"]) for row in result] == [30.0, 35.0]
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product_addfield_generated_nonempty', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_alter_field_generated_field_output_field_change_raises_clean_error(db_simple):
    """_alter_generated_field()'s guard only compared GENERATED_SQL (built purely from
    expression+stored) - an AlterField changing ONLY output_field (same expression/stored) gave
    a false negative (no ValueError raised), falling through into the ordinary ALTER-COLUMN-TYPE
    path. On Postgres, that path then sent `ALTER TABLE ... TYPE ... USING ...`, which Postgres
    itself rejected with a raw asyncpg.exceptions.InvalidColumnDefinitionError ("cannot specify
    USING when altering type of generated column") instead of this method's own intended,
    clear ValueError. Confirmed live before this fix. SQLite's own _alter_field() override never
    called this guard AT ALL - confirmed separately that it crashed via a different mechanism
    (_remake_table's column-mapping bug, covered by the sibling test below) rather than raising
    this ValueError, so this test's own SQLite branch just confirms the guard now at least fires
    there instead of being skipped entirely."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", DecimalField(max_digits=10, decimal_places=2)),
            ("quantity", IntField()),
        ],
        options={"table": "test_product_alter_generated_output_field"},
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_product_alter_generated_output_field"

    try:
        add_op = AddField(
            model_name="Product",
            name="total",
            field=GeneratedField(
                expression="price * quantity",
                output_field=IntField(),
            ),
        )
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        alter_op = AlterField(
            model_name="Product",
            name="total",
            field=GeneratedField(
                expression="price * quantity",
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        with pytest.raises(ConfigurationError, match="Modifying generated fields is not supported"):
            await alter_op.run("models", state, dry_run=False, state_editor=editor)
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_alter_field_generated_field_expression_change_raises_clean_error(db_simple):
    """_remake_table()'s own column-mapping builder re-added a just-excluded generated column
    for the alter_fields case (unlike the general surviving-columns loop, which correctly
    excludes it) - so on SQLite, ANY real AlterField on a GeneratedField (expression, stored, or
    output_field - anything past the safe-rename fast path) crashed with "cannot INSERT into
    generated column" during the rebuild's own INSERT...SELECT step, before
    _alter_generated_field()'s intended ValueError protection (which SQLite's own _alter_field()
    override never even called) got a chance to matter. Confirmed live before this fix - now
    raises the same clean ValueError as Postgres instead of a raw sqlite3.OperationalError."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("sqlite-specific _remake_table() column-mapping bug under test")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", DecimalField(max_digits=10, decimal_places=2)),
            ("quantity", IntField()),
        ],
        options={"table": "test_product_alter_generated_expression"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_product_alter_generated_expression", dialect)

    try:
        add_op = AddField(
            model_name="Product",
            name="total",
            field=GeneratedField(
                expression="price * quantity",
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await add_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(
            f"INSERT INTO {tbl} ({q('price', dialect)}, {q('quantity', dialect)}) VALUES (10, 3)"
        )

        alter_op = AlterField(
            model_name="Product",
            name="total",
            field=GeneratedField(
                expression="price * quantity * 2",
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        with pytest.raises(ConfigurationError, match="Modifying generated fields is not supported"):
            await alter_op.run("models", state, dry_run=False, state_editor=editor)
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product_alter_generated_expression', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_drops_a_single_field_constraint_that_referenced_it(db_simple):
    """RemoveField.state_forward() never cleaned up Meta.constraints/unique_together/indexes
    referencing the removed field - not GeneratedField-specific, reproduces identically for an
    ordinary field. On SQLite, _remake_table()'s own rebuild tried to re-emit
    `CONSTRAINT ... UNIQUE ("total")` in the new table's CREATE TABLE where that column no
    longer exists, crashing with "expressions prohibited in PRIMARY KEY and UNIQUE constraints".
    Confirmed live before this fix. Now the constraint - whose only reason to exist is the field
    just removed - is silently dropped instead, on every dialect."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("total", IntField()),
        ],
        options={"table": "test_widget_removefield_constraint", "constraints": [UniqueConstraint(fields=("total",))]},
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_widget_removefield_constraint"

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        remove_op = RemoveField(model_name="Widget", name="total")
        await remove_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Widget")]
        assert model_state.get_option_list("constraints") == []
        assert "total" not in model_state.fields
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_rejects_a_multi_field_constraint_that_still_references_it(db_simple):
    """A constraint spanning the removed field PLUS other, still-present fields is NOT silently
    narrowed or dropped - unlike the single-field case (the sibling test above), that would
    silently discard a uniqueness guarantee spanning those other fields too, a bigger surprise
    than a clear error. Raises ConfigurationError up front instead, before any DDL runs at all -
    the caller must explicitly remove/replace that constraint first."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="WidgetPair",
        fields=[
            ("id", IntField(primary_key=True)),
            ("a", IntField()),
            ("b", IntField()),
        ],
        options={
            "table": "test_widget_pair_removefield_constraint",
            "constraints": [UniqueConstraint(fields=("a", "b"))],
        },
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_widget_pair_removefield_constraint"

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        remove_op = RemoveField(model_name="WidgetPair", name="a")
        with pytest.raises(ConfigurationError, match="still referenced by constraint"):
            await remove_op.run("models", state, dry_run=False, state_editor=editor)

        # Rejected before state_forward() ever popped the field or touched the constraint list.
        model_state = state.models[("models", "WidgetPair")]
        assert "a" in model_state.fields
        assert len(model_state.get_option_list("constraints")) == 1
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_rejects_a_check_constraint_that_still_references_it(db_simple):
    """CheckConstraint.check is raw SQL, not a structured field list like UniqueConstraint.fields
    - RemoveField's own Meta-reference cleanup never recognized it at all, so a field still named
    by a CheckConstraint's check expression sailed through unblocked. Confirmed live before this
    fix: SQLite's _remake_table() rebuild then tried to re-emit `CHECK (quantity > 0)` in the new
    table, where "quantity" no longer exists, crashing with "no such column: quantity". Since a
    raw SQL string can't be reliably parsed for "does this ALSO reference some other still-
    present column", this always raises rather than ever silently dropping the constraint."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("quantity", IntField()),
        ],
        options={
            "table": "test_widget_removefield_check_constraint",
            "constraints": [CheckConstraint(check=RawSQLTerm("quantity > 0"), name="ck_widget_qty_positive")],
        },
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_widget_removefield_check_constraint"

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        remove_op = RemoveField(model_name="Widget", name="quantity")
        with pytest.raises(ConfigurationError, match="still referenced by constraint"):
            await remove_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Widget")]
        assert "quantity" in model_state.fields
        assert len(model_state.get_option_list("constraints")) == 1
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_field_updates_a_check_constraint_that_referenced_it(db_simple):
    """RenameField's own Meta-reference sync (_rename_meta_field_references) never touched
    CheckConstraint.check at all - the real DB-level CHECK expression is correctly rewritten by
    the database itself on RENAME COLUMN (tracked by internal column id, not name), but the
    tracked CheckConstraint object kept the OLD column name in its .check string forever,
    silently drifting from what the database actually enforces."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("quantity", IntField()),
        ],
        options={
            "table": "test_widget_renamefield_check_constraint",
            "constraints": [CheckConstraint(check=RawSQLTerm("quantity > 0"), name="ck_widget_qty_positive")],
        },
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_widget_renamefield_check_constraint"

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        rename_op = RenameField(model_name="Widget", old_name="quantity", new_name="qty")
        await rename_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Widget")]
        (constraint,) = model_state.get_option_list("constraints")
        assert constraint.check == RawSQLTerm("qty > 0")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_rejects_a_unique_constraint_condition_that_still_references_it(db_simple):
    """UniqueConstraint.condition is raw SQL (a partial-unique-index predicate), not a structured
    field list like UniqueConstraint.fields - a field named only by `condition`, not by `fields`
    itself, sailed through RemoveField's own Meta-reference cleanup unblocked before this fix.
    Postgres-only: a UniqueConstraint with a condition can't even be created on other dialects
    (schema_generator raises ConfigurationError for it), so this never reaches RemoveField at all
    anywhere else."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("UniqueConstraint.condition is Postgres-only")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("amount", IntField()),
            ("sku", TextField()),
        ],
        options={
            "table": "test_widget_removefield_unique_condition",
            "constraints": [
                UniqueConstraint(fields=("sku",), name="uq_widget_sku_positive", condition=RawSQLTerm("amount > 0"))
            ],
        },
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_widget_removefield_unique_condition"

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        remove_op = RemoveField(model_name="Widget", name="amount")
        with pytest.raises(ConfigurationError, match="still referenced by constraint"):
            await remove_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Widget")]
        assert "amount" in model_state.fields
        assert len(model_state.get_option_list("constraints")) == 1
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_field_updates_a_unique_constraint_condition_that_referenced_it(db_simple):
    """RenameField's own Meta-reference sync never touched UniqueConstraint.condition at all -
    same gap CheckConstraint.check had. Also exercises a field named in BOTH `fields` and
    `condition` at once, which the naive elif-with-early-return version of _renamed_constraint
    would have only updated one of."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("UniqueConstraint.condition is Postgres-only")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("amount", IntField()),
            ("sku", TextField()),
        ],
        options={
            "table": "test_widget_renamefield_unique_condition",
            "constraints": [
                UniqueConstraint(
                    fields=("amount", "sku"), name="uq_widget_amount_sku", condition=RawSQLTerm("amount > 0")
                )
            ],
        },
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_widget_renamefield_unique_condition"

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        rename_op = RenameField(model_name="Widget", old_name="amount", new_name="qty")
        await rename_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Widget")]
        (constraint,) = model_state.get_option_list("constraints")
        assert constraint.fields == ("qty", "sku")
        assert constraint.condition == RawSQLTerm("qty > 0")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_rejects_a_partial_index_q_condition_that_still_references_it(db_simple):
    """PartialIndex.condition can name a field that isn't among the index's own `.fields` at all
    (a predicate over a different column) - RemoveField's Meta-reference cleanup checks it too,
    not only `index.fields`."""
    from hare.ddl.indexes import PartialIndex

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("amount", IntField()),
            ("status", TextField()),
        ],
        options={
            "table": "test_widget_removefield_partial_idx_dict",
            "indexes": [
                PartialIndex(fields=("amount",), name="idx_widget_active_amount", condition=Q(status="active"))
            ],
        },
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_widget_removefield_partial_idx_dict"

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        remove_op = RemoveField(model_name="Widget", name="status")
        with pytest.raises(ConfigurationError, match="still referenced by the condition of PartialIndex"):
            await remove_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Widget")]
        assert "status" in model_state.fields
        assert len(model_state.get_option_list("indexes")) == 1
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_rejects_a_partial_index_raw_sql_condition_that_still_references_it(db_simple):
    """Same as the Q-condition sibling test above, but for a raw SQL condition - can't tell whether
    it ALSO names some other still-present column, so this never silently drops the index either,
    same reasoning as CheckConstraint.check."""
    from hare.ddl.indexes import PartialIndex

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("amount", IntField()),
            ("category", TextField()),
        ],
        options={
            "table": "test_widget_removefield_partial_idx_sql",
            "indexes": [
                PartialIndex(
                    fields=("category",), name="idx_widget_category_positive", condition=RawSQLTerm("amount > 0")
                )
            ],
        },
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_widget_removefield_partial_idx_sql"

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        remove_op = RemoveField(model_name="Widget", name="amount")
        with pytest.raises(ConfigurationError, match="still referenced by the condition of PartialIndex"):
            await remove_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Widget")]
        assert "amount" in model_state.fields
        assert len(model_state.get_option_list("indexes")) == 1
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_field_updates_a_partial_index_q_condition_that_referenced_it(db_simple):
    """RenameField's Meta-reference sync renames the field in a PartialIndex's Q condition - on
    SQLite, CREATE INDEX doesn't validate identifiers in a partial index's predicate, so a stale
    condition would silently create a broken index the next time the table is rebuilt."""
    from hare.ddl.indexes import PartialIndex

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("amount", IntField()),
            ("status", TextField()),
        ],
        options={
            "table": "test_widget_renamefield_partial_idx_dict",
            "indexes": [
                PartialIndex(fields=("amount",), name="idx_widget_active_amount", condition=Q(status="active"))
            ],
        },
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_widget_renamefield_partial_idx_dict"

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        rename_op = RenameField(model_name="Widget", old_name="status", new_name="state")
        await rename_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Widget")]
        (index,) = model_state.get_option_list("indexes")
        assert index.condition == Q(state="active")
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_field_updates_a_partial_index_raw_sql_condition_that_referenced_it(db_simple):
    """Same as the Q-condition sibling test above, but for a raw SQL condition, for a field named
    only by `condition`, not by the index's own `.fields`."""
    from hare.ddl.indexes import PartialIndex

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("amount", IntField()),
            ("category", TextField()),
        ],
        options={
            "table": "test_widget_renamefield_partial_idx_sql",
            "indexes": [
                PartialIndex(
                    fields=("category",), name="idx_widget_category_positive", condition=RawSQLTerm("amount > 0")
                )
            ],
        },
    )
    state = State(models={}, apps=StateApps())
    table_name = "test_widget_renamefield_partial_idx_sql"

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        rename_op = RenameField(model_name="Widget", old_name="amount", new_name="qty")
        await rename_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Widget")]
        (index,) = model_state.get_option_list("indexes")
        assert index.condition == RawSQLTerm("qty > 0")
        assert index.extra == " WHERE (qty > 0)"
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q(table_name, dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_index_on_field_with_custom_source_field_uses_real_column(db_simple):
    """Index(fields=...) referenced fields by their Python attribute name but built the CREATE
    INDEX SQL straight off that name too, ignoring the field's own source_field - the real DB
    column name, which the plain tuple/list Meta.indexes form already resolves correctly."""
    from hare.ddl.indexes import Index

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("PRAGMA index_info is SQLite-specific")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("eyedee", IntField(source_field="sometable_id")),
        ],
        options={
            "table": "test_index_source_field",
            "indexes": [Index(fields=("eyedee",), name="idx_test_source_field_eyedee")],
        },
    )
    state = State(models={}, apps=StateApps())

    async def indexed_columns() -> list[str]:
        rows = await conn.execute_dicts("PRAGMA index_info(idx_test_source_field_eyedee)")
        return [row["name"] for row in rows]

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        assert await indexed_columns() == ["sometable_id"]
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_index_source_field', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_remake_table_preserves_field_level_index(db_simple):
    """SQLite's _remake_table's post-rebuild index-recreation loop only iterated
    model._meta.indexes (explicit Meta.indexes entries) - it never checked a surviving field's
    own index=True/db_index=True the way the initial CREATE TABLE path's fields_with_index does,
    so that index silently vanished the moment any unrelated AlterField forced a rebuild."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("_remake_table is SQLite-specific")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("name", CharField(max_length=50)),
        ],
        options={"table": "test_field_index_remake"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        alter_op = AlterField(
            model_name="Widget",
            name="name",
            field=CharField(max_length=100, db_index=True),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_field_index_remake", dialect)
        rows = await conn.execute_dicts(f"PRAGMA index_list({tbl})")
        assert len(rows) == 1
        index_info = await conn.execute_dicts(f"PRAGMA index_info({q(rows[0]['name'], dialect)})")
        assert [row["name"] for row in index_info] == ["name"]
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_field_index_remake', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_remake_table_preserves_composite_primary_key(db_isolated):
    """SQLite's _remake_table never rendered a composite PRIMARY KEY constraint at all (unlike
    the CREATE TABLE path in BaseSchemaEditor, which already handles a CompositePrimaryKey's
    component fields - see test_create_model_composite_pk_gets_primary_key_constraint above) -
    the moment a rebuild ran for ANY unrelated reason (any other field add/alter/delete),
    uniqueness enforcement on a composite-PK table was silently and permanently dropped."""
    from tests.testmodels import CompositePkThing

    conn = db_isolated.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("_remake_table is SQLite-specific")

    editor = _get_schema_editor(conn)
    tbl = q(CompositePkThing._meta.db_table, dialect)

    await conn.execute_script(
        f"INSERT INTO {tbl} ({q('thing_id', dialect)}, {q('revision', dialect)}, {q('name', dialect)}) "
        "VALUES (1, 1, 'first')"
    )

    # A rebuild with no field changes - the mechanism under test, regardless of what real
    # operation would normally trigger it (any unrelated AlterField/RemoveField does).
    await editor._remake_table(CompositePkThing)

    with pytest.raises(Exception, match="UNIQUE constraint failed"):
        await conn.execute_script(
            f"INSERT INTO {tbl} ({q('thing_id', dialect)}, {q('revision', dialect)}, {q('name', dialect)}) "
            "VALUES (1, 1, 'duplicate')"
        )


# ---------------------------------------------------------------------------
# Trigger tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_alter_and_remove_trigger(db_simple):
    """AddTrigger fires on real writes; AlterTrigger changes its behavior in place; RemoveTrigger
    stops it firing - a portable body (plain INSERT into a second table, not a NEW.col:= assignment
    or a self-referencing UPDATE) so the same test exercises both backends identically."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    widget_tbl = q("test_widget_trig", dialect)
    audit_tbl = q("test_widget_trig_audit", dialect)
    # Postgres trigger functions must end on a RETURN statement; SQLite trigger bodies are plain
    # statement lists with no such requirement (and no RETURN statement at all).
    return_suffix = " RETURN NEW;" if dialect == "postgresql" else ""

    create_widget = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True)), ("name", CharField(max_length=50))],
        options={"table": "test_widget_trig"},
    )
    create_audit = CreateModel(
        name="WidgetAudit",
        fields=[("id", IntField(primary_key=True)), ("note", CharField(max_length=50))],
        options={"table": "test_widget_trig_audit"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_widget.run("models", state, dry_run=False, state_editor=editor)
        await create_audit.run("models", state, dry_run=False, state_editor=editor)

        # Body inserts into the audit table, not the trigger's own table - avoids any
        # dialect-specific recursive-trigger behavior entirely.
        trigger = Trigger(
            name="widget_trig_on_update",
            on=TriggerEvent.UPDATE,
            body=f"INSERT INTO {audit_tbl} ({q('note', dialect)}) VALUES ('updated');{return_suffix}",
        )
        add_op = AddTrigger(model_name="Widget", trigger=trigger)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        await conn.execute_script(f"INSERT INTO {widget_tbl} ({q('name', dialect)}) VALUES ('foo')")
        await conn.execute_script(f"UPDATE {widget_tbl} SET {q('name', dialect)} = 'bar' WHERE id = 1")
        rows = await conn.execute_dicts(f"SELECT note FROM {audit_tbl}")
        assert [row["note"] for row in rows] == ["updated"]

        altered = Trigger(
            name="widget_trig_on_update",
            on=TriggerEvent.UPDATE,
            body=f"INSERT INTO {audit_tbl} ({q('note', dialect)}) VALUES ('altered');{return_suffix}",
        )
        alter_op = AlterTrigger(model_name="Widget", trigger=altered)
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        await conn.execute_script(f"UPDATE {widget_tbl} SET {q('name', dialect)} = 'baz' WHERE id = 1")
        rows = await conn.execute_dicts(f"SELECT note FROM {audit_tbl} ORDER BY id")
        assert [row["note"] for row in rows] == ["updated", "altered"]

        remove_op = RemoveTrigger(model_name="Widget", name="widget_trig_on_update")
        await remove_op.run("models", state, dry_run=False, state_editor=editor)

        await conn.execute_script(f"UPDATE {widget_tbl} SET {q('name', dialect)} = 'qux' WHERE id = 1")
        rows = await conn.execute_dicts(f"SELECT note FROM {audit_tbl} ORDER BY id")
        assert [row["note"] for row in rows] == ["updated", "altered"]  # unchanged - trigger is gone
    finally:
        for table in ("test_widget_trig", "test_widget_trig_audit"):
            try:
                await conn.execute_script(f"DROP TABLE IF EXISTS {q(table, dialect)}")
            except Exception:
                pass


@pytest.mark.asyncio
async def test_add_trigger_with_quote_in_name_is_escaped(db_simple):
    """Regression test: Trigger.name containing a literal '"' must go through self.quote() like
    every other identifier (table/column/index/constraint names already do) - the trigger DDL
    templates used to hardcode a literal '"..."' wrapper around the raw name, so an embedded
    quote broke out of the quoted identifier and corrupted the generated DDL (a genuine SQL-
    identifier-injection shape: unescaped, a name like 'evil"; DROP TABLE ...; --' would splice
    attacker SQL straight into the CREATE TRIGGER statement)."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    widget_tbl = q("test_widget_trig_quote", dialect)
    return_suffix = " RETURN NEW;" if dialect == "postgresql" else ""
    trigger_name = 'widget_trig_"quote'

    create_widget = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True)), ("name", CharField(max_length=50))],
        options={"table": "test_widget_trig_quote"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_widget.run("models", state, dry_run=False, state_editor=editor)

        trigger = Trigger(
            name=trigger_name,
            on=TriggerEvent.INSERT,
            body=f"UPDATE {widget_tbl} SET {q('name', dialect)} = 'hit' WHERE id = NEW.id;{return_suffix}",
        )

        # First, capture the generated DDL WITHOUT executing it (collect_sql=True) to assert the
        # embedded quote is actually doubled in the output text - not just that execution happens
        # to succeed.
        model = state.apps.get_model("models.Widget")
        collecting_editor = _get_schema_editor(conn)
        collecting_editor.collect_sql = True
        await collecting_editor.add_trigger(model, trigger)
        generated_sql = "\n".join(collecting_editor.collected_sql)
        expected_quoted_name = collecting_editor.quote(trigger_name)
        assert f"CREATE TRIGGER {expected_quoted_name}" in generated_sql, (
            f"embedded quote in trigger name wasn't escaped like other identifiers: {generated_sql!r}"
        )

        # Then run it for real through the actual operation, proving the escaped DDL both
        # executes successfully and the trigger fires under its real (quote-containing) name.
        add_op = AddTrigger(model_name="Widget", trigger=trigger)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        await conn.execute_script(f"INSERT INTO {widget_tbl} ({q('name', dialect)}) VALUES ('foo')")
        rows = await conn.execute_dicts(f"SELECT name FROM {widget_tbl}")
        assert rows == [{"name": "hit"}], "trigger with a quote in its name didn't fire correctly"

        remove_op = RemoveTrigger(model_name="Widget", name=trigger_name)
        await remove_op.run("models", state, dry_run=False, state_editor=editor)
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {widget_tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_postgres_trigger_manages_backing_function(db_simple):
    """On Postgres, AddTrigger creates a real, separately-named function; RemoveTrigger drops it
    again - not just the trigger attachment."""
    conn = db_simple.db()
    if conn.dialect.name != "postgresql":
        pytest.skip("function+trigger pairing is Postgres-specific")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True))],
        options={"table": "test_widget_trig_fn"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        trigger = Trigger(name="widget_trig_fn_check", on=TriggerEvent.INSERT, body="RETURN NEW;")
        add_op = AddTrigger(model_name="Widget", trigger=trigger)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts("SELECT proname FROM pg_proc WHERE proname = 'widget_trig_fn_check_fn'")
        assert len(rows) == 1

        remove_op = RemoveTrigger(model_name="Widget", name="widget_trig_fn_check")
        await remove_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts("SELECT proname FROM pg_proc WHERE proname = 'widget_trig_fn_check_fn'")
        assert rows == []
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_widget_trig_fn" CASCADE')
            await conn.execute_script("DROP FUNCTION IF EXISTS widget_trig_fn_check_fn()")
        except Exception:
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_deferred", [True, False])
async def test_postgres_constraint_trigger_deferral_semantics(db_simple, initially_deferred):
    """AddTrigger with deferrable=True emits a real CREATE CONSTRAINT TRIGGER ... DEFERRABLE -
    verify pg_trigger's own tgdeferrable/tginitdeferred columns reflect it (not just that the
    DDL executed without error), and that RemoveTrigger drops it like any other trigger."""
    conn = db_simple.db()
    if conn.dialect.name != "postgresql":
        pytest.skip("CONSTRAINT TRIGGER is Postgres-specific")
    editor = _get_schema_editor(conn)

    trigger_name = f"widget_constraint_trig_{'deferred' if initially_deferred else 'immediate'}"
    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True))],
        options={"table": "test_widget_constraint_trig"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        trigger = Trigger(
            name=trigger_name,
            on=TriggerEvent.INSERT,
            body="RETURN NEW;",
            deferrable=True,
            initially_deferred=initially_deferred,
        )
        add_op = AddTrigger(model_name="Widget", trigger=trigger)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(
            "SELECT tgdeferrable, tginitdeferred FROM pg_trigger WHERE tgname = $1", [trigger_name]
        )
        assert len(rows) == 1
        assert rows[0]["tgdeferrable"] is True
        assert rows[0]["tginitdeferred"] is initially_deferred

        remove_op = RemoveTrigger(model_name="Widget", name=trigger_name)
        await remove_op.run("models", state, dry_run=False, state_editor=editor)
        rows = await conn.execute_dicts("SELECT 1 FROM pg_trigger WHERE tgname = $1", [trigger_name])
        assert rows == []
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_widget_constraint_trig" CASCADE')
            await conn.execute_script(f"DROP FUNCTION IF EXISTS {trigger_name}_fn()")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_constraint_trigger_not_supported(db_simple):
    """SQLite has no CONSTRAINT TRIGGER equivalent at all - deferrable=True must fail loudly
    rather than silently falling back to a plain (non-deferrable) trigger."""
    conn = db_simple.db()
    if conn.dialect.name != "sqlite":
        pytest.skip("CONSTRAINT TRIGGER is unsupported specifically on SQLite")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True))],
        options={"table": "test_widget_trig_deferrable"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        trigger = Trigger(
            name="widget_trig_deferrable",
            on=TriggerEvent.INSERT,
            body="SELECT 1;",
            deferrable=True,
        )
        add_op = AddTrigger(model_name="Widget", trigger=trigger)
        with pytest.raises(UnSupportedError):
            await add_op.run("models", state, dry_run=False, state_editor=editor)
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_widget_trig_deferrable"')
        except Exception:
            pass


@pytest.mark.asyncio
async def test_delete_model_drops_orphaned_trigger_function(db_simple):
    """DeleteModel's DROP TABLE ... CASCADE removes the trigger itself, but its backing function
    is a separate catalog object - DeleteModel must drop that explicitly too."""
    conn = db_simple.db()
    if conn.dialect.name != "postgresql":
        pytest.skip("function+trigger pairing is Postgres-specific")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True))],
        options={
            "table": "test_widget_trig_del",
            "triggers": [Trigger(name="widget_trig_del_check", on=TriggerEvent.INSERT, body="RETURN NEW;")],
        },
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        rows = await conn.execute_dicts("SELECT proname FROM pg_proc WHERE proname = 'widget_trig_del_check_fn'")
        assert len(rows) == 1

        delete_op = DeleteModel(name="Widget")
        await delete_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts("SELECT proname FROM pg_proc WHERE proname = 'widget_trig_del_check_fn'")
        assert rows == []
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_widget_trig_del" CASCADE')
            await conn.execute_script("DROP FUNCTION IF EXISTS widget_trig_del_check_fn()")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_trigger_body_with_internal_semicolons(db_simple):
    """Regression test: SqliteSchemaEditor._run_sql() naively splits DDL on every ";", which
    would otherwise mangle a multi-statement CREATE TRIGGER ... BEGIN a; b; END body - add_trigger()
    must bypass that splitting."""
    conn = db_simple.db()
    if conn.dialect.name != "sqlite":
        pytest.skip("_run_sql()'s naive splitting is SQLite-specific")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True)), ("hits", IntField(default=0)), ("touches", IntField(default=0))],
        options={"table": "test_widget_trig_multi"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        trigger = Trigger(
            name="widget_trig_multi_stmt",
            on=TriggerEvent.UPDATE,
            timing=TriggerTiming.BEFORE,
            body=(
                "UPDATE test_widget_trig_multi SET hits = hits + 1 WHERE id = OLD.id;\n"
                "    UPDATE test_widget_trig_multi SET touches = touches + 1 WHERE id = OLD.id;"
            ),
        )
        add_op = AddTrigger(model_name="Widget", trigger=trigger)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        await conn.execute_script('INSERT INTO "test_widget_trig_multi" (id, hits, touches) VALUES (1, 0, 0)')
        await conn.execute_script('UPDATE "test_widget_trig_multi" SET id = 1 WHERE id = 1')
        rows = await conn.execute_dicts('SELECT hits, touches FROM "test_widget_trig_multi" WHERE id = 1')
        assert rows == [{"hits": 1, "touches": 1}]
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_widget_trig_multi"')
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_statement_level_trigger_not_supported(db_simple):
    conn = db_simple.db()
    if conn.dialect.name != "sqlite":
        pytest.skip("STATEMENT-level triggers are unsupported specifically on SQLite")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True))],
        options={"table": "test_widget_trig_stmt"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        trigger = Trigger(
            name="widget_trig_stmt", on=TriggerEvent.INSERT, for_each=TriggerForEach.STATEMENT, body="SELECT 1;"
        )
        add_op = AddTrigger(model_name="Widget", trigger=trigger)
        with pytest.raises(UnSupportedError):
            await add_op.run("models", state, dry_run=False, state_editor=editor)
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_widget_trig_stmt"')
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_remake_table_preserves_trigger(db_simple):
    """Regression test: SQLite drops a trigger automatically when its table is dropped -
    _remake_table() (used by AlterField/RemoveField/add_constraint/remove_constraint) must
    recreate every Meta.trigger after rebuilding the table, or it silently vanishes."""
    conn = db_simple.db()
    if conn.dialect.name != "sqlite":
        pytest.skip("_remake_table is SQLite-specific")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", IntField(null=False)),
            ("label", CharField(max_length=50, null=True)),
            ("hits", IntField(default=0)),
        ],
        options={
            "table": "test_widget_remake_trig",
            "triggers": [
                Trigger(
                    name="widget_remake_trig_bump",
                    on=TriggerEvent.UPDATE,
                    timing=TriggerTiming.BEFORE,
                    body="UPDATE test_widget_remake_trig SET hits = hits + 1 WHERE id = OLD.id;",
                )
            ],
        },
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        # An unrelated AlterField (not a simple rename) forces _remake_table().
        alter_op = AlterField(
            model_name="Widget",
            name="label",
            field=CharField(max_length=100, null=True),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'widget_remake_trig_bump'"
        )
        assert len(rows) == 1, "trigger was lost when _remake_table() rebuilt the table"

        tbl = q("test_widget_remake_trig", "sqlite")
        await conn.execute_script(f"INSERT INTO {tbl} (id, price, hits) VALUES (1, 100, 0)")
        await conn.execute_script(f"UPDATE {tbl} SET price = 200 WHERE id = 1")
        rows = await conn.execute_dicts(f"SELECT hits FROM {tbl} WHERE id = 1")
        assert rows == [{"hits": 1}], "recreated trigger doesn't actually fire"
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_widget_remake_trig"')
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_field_updates_a_trigger_that_referenced_it(db_simple):
    """RenameField's own Meta-reference sync never touched Trigger.body/.when/.on at all - same
    raw-SQL gap CheckConstraint.check/UniqueConstraint.condition/PartialIndex.condition already
    had fixed. SQLite itself rewrites a trigger's real DDL on RENAME COLUMN, but the tracked
    Trigger object kept the OLD column name in .when/.body forever - confirmed live before this
    fix that a later, unrelated _remake_table() rebuild (any AlterField/RemoveField on a
    different column) then recreated the trigger from that STALE state, referencing a column
    that no longer exists - "no such column: NEW.quantity" on every subsequent UPDATE, not just
    ones touching the renamed column."""
    conn = db_simple.db()
    if conn.dialect.name != "sqlite":
        pytest.skip("SQLite-specific trigger body syntax (RAISE(ABORT, ...))")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True)), ("quantity", IntField())],
        options={
            "table": "test_widget_trig_rename",
            "triggers": [
                Trigger(
                    name="widget_trig_rename_qty_check",
                    on=TriggerEvent.UPDATE,
                    timing=TriggerTiming.BEFORE,
                    when="NEW.quantity <= 0",
                    body="SELECT RAISE(ABORT, 'bad qty') WHERE NEW.quantity <= 0;",
                )
            ],
        },
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        rename_op = RenameField(model_name="Widget", old_name="quantity", new_name="qty")
        await rename_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Widget")]
        (trigger,) = model_state.get_option_list("triggers")
        assert trigger.when == "NEW.qty <= 0"
        assert "NEW.qty" in trigger.body

        # A later, unrelated rebuild (AlterField on a different column) recreates the trigger
        # from tracked state - must use the RENAMED column, not crash on the old one.
        alter_op = AlterField(model_name="Widget", name="id", field=IntField(primary_key=True))
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_widget_trig_rename", "sqlite")
        await conn.execute_script(f"INSERT INTO {tbl} (id, qty) VALUES (1, 5)")
        await conn.execute_script(f"UPDATE {tbl} SET qty = 10 WHERE id = 1")
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_widget_trig_rename"')
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_rejects_a_trigger_that_still_references_it(db_simple):
    """RemoveField never checked Meta.triggers at all - a field still named by a trigger's
    .when/.body/.on sailed through unblocked. Confirmed live before this fix: the trigger
    survived RemoveField verbatim (still referencing the now-gone column), and the very next
    _remake_table() rebuild (RemoveField's own rebuild, in this case) recreated it from that
    stale text - breaking EVERY subsequent UPDATE on the table, even ones that never touch the
    removed column, with "no such column: NEW.quantity"."""
    conn = db_simple.db()
    if conn.dialect.name != "sqlite":
        pytest.skip("SQLite-specific trigger body syntax (RAISE(ABORT, ...))")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True)), ("quantity", IntField())],
        options={
            "table": "test_widget_trig_removefield",
            "triggers": [
                Trigger(
                    name="widget_trig_removefield_qty_check",
                    on=TriggerEvent.UPDATE,
                    timing=TriggerTiming.BEFORE,
                    when="NEW.quantity <= 0",
                    body="SELECT RAISE(ABORT, 'bad qty') WHERE NEW.quantity <= 0;",
                )
            ],
        },
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        remove_op = RemoveField(model_name="Widget", name="quantity")
        with pytest.raises(ConfigurationError, match="still referenced by trigger"):
            await remove_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Widget")]
        assert "quantity" in model_state.fields
        assert len(model_state.get_option_list("triggers")) == 1
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_widget_trig_removefield"')
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_rejects_a_generatedfield_expression_that_still_references_it(db_simple):
    """RemoveField never checked sibling fields at all - a GeneratedField's own `expression`
    (raw SQL) still naming the field about to be removed sailed through unblocked. Confirmed
    live before this fix: on SQLite, _remake_table() crashed with a raw "no such column: price"
    (the rebuilt table's copied-over GENERATED ALWAYS AS clause still named the gone column) -
    not even a clean error, an uncaught OperationalError."""
    conn = db_simple.db()
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="GenExprParent",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", IntField()),
            ("quantity", IntField()),
            (
                "total",
                GeneratedField(
                    expression="price * quantity", output_field=DecimalField(max_digits=12, decimal_places=2)
                ),
            ),
        ],
        options={"table": "test_genexpr_removefield"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        remove_op = RemoveField(model_name="GenExprParent", name="price")
        with pytest.raises(ConfigurationError, match="referenced by GeneratedField 'total'"):
            await remove_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "GenExprParent")]
        assert "price" in model_state.fields
        assert "total" in model_state.fields
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_genexpr_removefield"')
        except Exception:
            pass


@pytest.mark.asyncio
async def test_remove_field_rejects_a_tsvectorfield_source_field_that_still_references_it(db_simple):
    """Same gap as the GeneratedField case above, for TSVectorField's own `source_fields` (a
    structured tuple, not raw SQL) - RemoveField never checked it either. Confirmed live before
    this fix on Postgres: the column's own generated-column dependency isn't visible to
    RemoveField's own DDL guard at all, so DROP COLUMN ... CASCADE (base.py's own
    DELETE_FIELD_TEMPLATE) silently dropped the dependent search_vector column - and its GIN
    index - out from under the ORM's state entirely, with no error anywhere."""
    conn = db_simple.db()
    if conn.dialect.name != "postgresql":
        pytest.skip("TSVectorField is Postgres-only")
    from hare.dialects.postgresql.fields.search import TSVectorField
    from hare.dialects.postgresql.indexes import GinIndex

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Doc",
        fields=[
            ("id", IntField(primary_key=True)),
            ("body", TextField()),
            ("search_vector", TSVectorField(source_fields=("body",), config="english", stored=True)),
        ],
        options={"table": "test_gen_tsv_source_gap", "indexes": [GinIndex(fields=["search_vector"])]},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        remove_op = RemoveField(model_name="Doc", name="body")
        with pytest.raises(ConfigurationError, match="TSVectorField 'search_vector'"):
            await remove_op.run("models", state, dry_run=False, state_editor=editor)

        model_state = state.models[("models", "Doc")]
        assert "body" in model_state.fields
        assert "search_vector" in model_state.fields

        _, rows = await conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'test_gen_tsv_source_gap'"
        )
        columns = {r["column_name"] for r in rows}
        assert {"body", "search_vector"} <= columns
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_gen_tsv_source_gap"')
        except Exception:
            pass


@pytest.mark.asyncio
async def test_postgres_trigger_function_is_schema_qualified(db_simple):
    """Regression test: a trigger's backing function must live in the model's own Meta.schema,
    not always in public - otherwise two tenants' same-named trigger functions collide, and
    drop_schema() never cleans up a function it never owned."""
    conn = db_simple.db()
    if conn.dialect.name != "postgresql":
        pytest.skip("schemas are Postgres-specific")
    editor = _get_schema_editor(conn)

    schema_name = "test_trig_schema_qual"
    await conn.execute_script(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
    await editor.create_schema(schema_name)

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True))],
        options={
            "table": "test_widget_schema_trig",
            "schema": schema_name,
            "triggers": [Trigger(name="widget_schema_trig_check", on=TriggerEvent.INSERT, body="RETURN NEW;")],
        },
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE p.proname = 'widget_schema_trig_check_fn' AND n.nspname = $1",
            [schema_name],
        )
        assert len(rows) == 1, "trigger function wasn't created inside Meta.schema"

        public_rows = await conn.execute_dicts(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE p.proname = 'widget_schema_trig_check_fn' AND n.nspname = 'public'"
        )
        assert public_rows == [], "trigger function leaked into public instead of Meta.schema"

        # Dropping the schema now cleans up the function too - it was never left orphaned in public.
        await conn.execute_script(f'DROP SCHEMA "{schema_name}" CASCADE')
        rows = await conn.execute_dicts("SELECT proname FROM pg_proc WHERE proname = 'widget_schema_trig_check_fn'")
        assert rows == []
    finally:
        try:
            await conn.execute_script(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_trigger_renames_function_too(db_simple):
    """RenameTrigger must produce a real, detectable rename (not remove+add) for a pure name
    change, and (Postgres only) rename the backing function to match the new name's
    f"{name}_fn" convention."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    body = "RETURN NEW;" if dialect == "postgresql" else "SELECT 1;"

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True))],
        options={
            "table": "test_widget_rename_trig",
            "triggers": [Trigger(name="widget_trig_old_name", on=TriggerEvent.INSERT, body=body)],
        },
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        rename_op = RenameTrigger(
            model_name="Widget", old_name="widget_trig_old_name", new_name="widget_trig_new_name"
        )
        await rename_op.run("models", state, dry_run=False, state_editor=editor)

        tbl = q("test_widget_rename_trig", dialect)
        # The renamed trigger still works.
        await conn.execute_script(f"INSERT INTO {tbl} (id) VALUES (1)")

        if dialect == "sqlite":
            rows = await conn.execute_dicts(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'widget_trig_old_name'"
            )
            assert rows == []
            rows = await conn.execute_dicts(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'widget_trig_new_name'"
            )
            assert len(rows) == 1
        else:
            rows = await conn.execute_dicts("SELECT tgname FROM pg_trigger WHERE tgname = 'widget_trig_old_name'")
            assert rows == []
            rows = await conn.execute_dicts("SELECT tgname FROM pg_trigger WHERE tgname = 'widget_trig_new_name'")
            assert len(rows) == 1
            # The backing function was renamed too, not left behind under the old name.
            rows = await conn.execute_dicts("SELECT proname FROM pg_proc WHERE proname = 'widget_trig_old_name_fn'")
            assert rows == []
            rows = await conn.execute_dicts("SELECT proname FROM pg_proc WHERE proname = 'widget_trig_new_name_fn'")
            assert len(rows) == 1
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_widget_rename_trig" CASCADE')
            if dialect == "postgresql":
                await conn.execute_script("DROP FUNCTION IF EXISTS widget_trig_new_name_fn()")
                await conn.execute_script("DROP FUNCTION IF EXISTS widget_trig_old_name_fn()")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_trigger_backward_renames_back(db_simple):
    """RenameTrigger.database_backward looked up self.old_name in old_state and self.new_name in
    new_state - the SAME lookup database_forward uses. migration.py swaps the meaning of these
    two params for a backward run though: old_state is the state we're rolling back FROM (already
    forward-applied, has the trigger under self.new_name), new_state is the state we're rolling
    back TO (has it under self.old_name) - so the old code searched for self.old_name in a state
    that only had self.new_name, guaranteed IncompatibleStateError on ANY rollback of a migration
    containing a RenameTrigger."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    body = "RETURN NEW;" if dialect == "postgresql" else "SELECT 1;"

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True))],
        options={
            "table": "test_widget_rename_trig_bwd",
            "triggers": [Trigger(name="widget_trig_old_bwd", on=TriggerEvent.INSERT, body=body)],
        },
    )
    state = State(models={}, apps=StateApps())
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        rename_op = RenameTrigger(model_name="Widget", old_name="widget_trig_old_bwd", new_name="widget_trig_new_bwd")
        old_state = state.clone()
        new_state = old_state.clone()
        rename_op.state_forward("models", new_state)
        await rename_op.database_forward("models", old_state, new_state, editor)

        # Backward: rolling back FROM new_state (current, has the new name) TO old_state (target,
        # old name) - matches migration.py's own _run_database_backward(operation, from_state,
        # to_state, ...) -> operation.database_backward(app_label, old_state=from_state, ...).
        await rename_op.database_backward("models", new_state, old_state, editor)

        tbl = q("test_widget_rename_trig_bwd", dialect)
        await conn.execute_script(f"INSERT INTO {tbl} (id) VALUES (1)")

        if dialect == "sqlite":
            rows = await conn.execute_dicts(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'widget_trig_old_bwd'"
            )
            assert len(rows) == 1
            rows = await conn.execute_dicts(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'widget_trig_new_bwd'"
            )
            assert rows == []
        else:
            rows = await conn.execute_dicts("SELECT tgname FROM pg_trigger WHERE tgname = 'widget_trig_old_bwd'")
            assert len(rows) == 1
            rows = await conn.execute_dicts("SELECT tgname FROM pg_trigger WHERE tgname = 'widget_trig_new_bwd'")
            assert rows == []
    finally:
        try:
            await conn.execute_script('DROP TABLE IF EXISTS "test_widget_rename_trig_bwd" CASCADE')
        except Exception:
            pass


@pytest.mark.asyncio
async def test_create_model_composite_pk_gets_primary_key_constraint(db_isolated):
    """A CompositePrimaryKey's component fields never carry primary_key=True individually (each is
    declared as a plain field - CompositePrimaryKey just names which existing fields form the
    key), so CREATE_MODEL's per-field loop never emitted a PRIMARY KEY column suffix for any of
    them - and nothing else added a table-level PRIMARY KEY constraint either. The table was
    created with NO primary key at all: duplicate "composite key" rows were silently accepted."""
    from tests.testmodels import CompositePkThing

    conn = db_isolated.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    tbl = q(CompositePkThing._meta.db_table, dialect)

    # db_isolated already generated schema for every tests.testmodels model (including this one)
    # via the separate hare/backends/*/schema_generator.py path (not the migrations schema editor
    # under test here) - drop it first so create_model() below builds it fresh through the code
    # path this test actually targets.
    await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
    try:
        await editor.create_model(CompositePkThing)
        await conn.execute_script(
            f"INSERT INTO {tbl} ({q('thing_id', dialect)}, {q('revision', dialect)}, {q('name', dialect)}) "
            "VALUES (1, 1, 'first')"
        )
        with pytest.raises(Exception):
            await conn.execute_script(
                f"INSERT INTO {tbl} ({q('thing_id', dialect)}, {q('revision', dialect)}, {q('name', dialect)}) "
                "VALUES (1, 1, 'duplicate')"
            )
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_composite_pk_constraint_name_matches_between_generate_schemas_and_migrate(db_isolated):
    """generate_schemas() (hare/backends/base/schema_generator.py) used to emit an UNNAMED
    "PRIMARY KEY (a, b)" for a composite PK, leaving Postgres to auto-name it "<table>_pkey" -
    while the migrations schema_editor's CreateModel path always emitted an explicitly-named
    "CONSTRAINT pk_<table>_<cols>_<hash> PRIMARY KEY (...)" for the exact same model. Same
    columns, same enforcement, but a different constraint name depending purely on which code
    path created the table. generate_schemas() now reuses the same named-constraint helper, so
    both paths must agree."""
    conn = db_isolated.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "postgresql":
        pytest.skip("Postgres auto-names an unnamed composite PK - no catalog-observable naming difference on sqlite")

    from tests.testmodels import CompositePkThing

    async def pk_constraint_name() -> str:
        rows = await conn.execute_dicts(
            "SELECT conname FROM pg_constraint WHERE conrelid = "
            f"'{CompositePkThing._meta.db_table}'::regclass AND contype = 'p'"
        )
        assert len(rows) == 1
        return rows[0]["conname"]

    editor = _get_schema_editor(conn)
    tbl = q(CompositePkThing._meta.db_table, dialect)

    try:
        # db_isolated already generated this table via generate_schemas() at fixture setup.
        name_from_generate_schemas = await pk_constraint_name()

        await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        await editor.create_model(CompositePkThing)
        name_from_migrate = await pk_constraint_name()

        assert name_from_generate_schemas == name_from_migrate
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_create_model_operation_reconstructs_composite_primary_key(db_simple):
    """Unlike test_create_model_composite_pk_gets_primary_key_constraint above (which calls
    editor.create_model() on a real, already-declared model class), this goes through the actual
    CreateModel migration OPERATION - the code path an autodetector-generated migration runs.
    CreateModel.model reconstructs a live Model class from `fields`/`options` alone; it used to
    pass pk_attr through a synthetic Meta only, which the metaclass's PK-detection never reads,
    so the reconstructed model silently fell back to a surrogate "id" PK with no composite
    enforcement in the DDL at all."""
    from hare.exceptions import IntegrityError

    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    tbl = q("test_thing_revision", dialect)

    create_op = CreateModel(
        name="ThingRevision",
        fields=[
            ("thing_id", IntField()),
            ("revision", IntField()),
            ("name", CharField(max_length=50)),
        ],
        options={"table": "test_thing_revision", "pk_attr": ("thing_id", "revision")},
    )
    state = State(models={}, apps=StateApps())

    try:
        assert create_op.model._meta.pk_attr == ("thing_id", "revision")

        await create_op.run("models", state, dry_run=False, state_editor=editor)

        await conn.execute_script(
            f"INSERT INTO {tbl} ({q('thing_id', dialect)}, {q('revision', dialect)}, {q('name', dialect)}) "
            "VALUES (1, 1, 'first')"
        )
        with pytest.raises(IntegrityError):
            await conn.execute_script(
                f"INSERT INTO {tbl} ({q('thing_id', dialect)}, {q('revision', dialect)}, {q('name', dialect)}) "
                "VALUES (1, 1, 'duplicate')"
            )
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_composite_target_fk_migration_lifecycle(db_isolated):
    """Full migration-path lifecycle for a composite-target FK: CreateModel emits a real
    table-level FOREIGN KEY constraint on both dialects; RemoveField/AddField on an existing
    table drop/re-add it correctly too (SQLite via a full table rebuild -
    SqliteSchemaEditor.add_field()/remove_field() route composite targets through
    _remake_table(); Postgres via a real multi-column ALTER TABLE ADD/DROP). Closes the loop
    test_composite_primary_key.py's generate_schemas()-only DDL tests left open - this is the
    other DDL hierarchy (migrations), which used to duplicate the same single-column-only
    REFERENCES logic independently."""
    from tests.testmodels import DocumentRevisionNote, VersionedDocument

    conn = db_isolated.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    parent_tbl = q(VersionedDocument._meta.db_table, dialect)
    child_tbl = q(DocumentRevisionNote._meta.db_table, dialect)

    # db_isolated already created both tables via generate_schemas() - drop them so create_model()
    # below builds the child fresh through the migrations code path this test actually targets.
    await conn.execute_script(f"DROP TABLE IF EXISTS {child_tbl}")
    await conn.execute_script(f"DROP TABLE IF EXISTS {parent_tbl}")
    try:
        await editor.create_model(VersionedDocument)
        await editor.create_model(DocumentRevisionNote)

        doc_id = "11111111-1111-1111-1111-111111111111"
        await conn.execute_script(
            f'INSERT INTO {parent_tbl} ("id", "version", "title") VALUES (\'{doc_id}\', 1, \'v1\')'
        )
        await conn.execute_script(
            f'INSERT INTO {child_tbl} ("document_id", "document_version", "note") '
            f"VALUES ('{doc_id}', 1, 'looks good')"
        )

        with pytest.raises(Exception):
            await conn.execute_script(
                f'INSERT INTO {child_tbl} ("document_id", "document_version", "note") '
                "VALUES ('22222222-2222-2222-2222-222222222222', 1, 'orphan')"
            )

        # RemoveField: drop the composite FK entirely (clearing the one row first - it's not
        # nullable, so re-adding it below with existing NULL-backfilled data would itself
        # violate NOT NULL, an orthogonal concern this test isn't about).
        await conn.execute_script(f"DELETE FROM {child_tbl}")
        document_field = DocumentRevisionNote._meta.fields_map["document"]
        await editor.remove_field(DocumentRevisionNote, document_field)
        # A SELECT of a nonexistent double-quoted "document_id" wouldn't reliably fail on SQLite
        # (a double-quoted identifier with no matching column silently falls back to a string
        # literal there) - inserting a row using only the surviving columns and checking its
        # returned shape is the portable way to confirm the columns are actually gone.
        await conn.execute_script(f"INSERT INTO {child_tbl} (\"note\") VALUES ('no fk yet')")
        rows = await conn.execute_dicts(f"SELECT * FROM {child_tbl}")
        assert len(rows) == 1
        assert "document_id" not in rows[0]
        assert "document_version" not in rows[0]
        await conn.execute_script(f"DELETE FROM {child_tbl}")

        # AddField: add it back to the now-stripped table, and the real constraint must be
        # enforced again immediately.
        await editor.add_field(DocumentRevisionNote, "document")
        await conn.execute_script(
            f'INSERT INTO {child_tbl} ("document_id", "document_version", "note") '
            f"VALUES ('{doc_id}', 1, 'back again')"
        )
        with pytest.raises(Exception):
            await conn.execute_script(
                f'INSERT INTO {child_tbl} ("document_id", "document_version", "note") '
                "VALUES ('33333333-3333-3333-3333-333333333333', 1, 'orphan-again')"
            )

        # AlterField: change on_delete (CASCADE -> RESTRICT) on the composite FK and verify the
        # real constraint was replaced, not just left as CASCADE - deleting the still-referenced
        # parent row must now be rejected instead of cascading.
        from copy import copy as copy_field

        old_document_field = DocumentRevisionNote._meta.fields_map["document"]
        new_document_field = copy_field(old_document_field)
        new_document_field.on_delete = RESTRICT
        if dialect == "sqlite":
            await editor._alter_field(DocumentRevisionNote, old_document_field, new_document_field)
        else:
            await editor._alter_fk_on_delete(
                DocumentRevisionNote, old_document_field.source_fields[0], old_document_field, new_document_field
            )

        with pytest.raises(Exception):
            await conn.execute_script(f"DELETE FROM {parent_tbl} WHERE \"id\" = '{doc_id}'")
        # The still-referencing row must have survived the rejected delete attempt. This exact
        # "SELECT * FROM {child_tbl}" text was ALSO used earlier in this test (before the
        # RemoveField/AddField pair changed the table's column set) - rust.pg's connection-level
        # statement cache used to reuse the stale plan from that earlier call and have Postgres
        # reject it with "cached plan must not change result type" (SQLSTATE 0A000). Now covered
        # by rust/pg/src/client.rs's prepare_and_run_with_stale_plan_retry - kept as SELECT *
        # (not COUNT(*)) deliberately, to keep exercising that exact regression.
        rows = await conn.execute_dicts(f"SELECT * FROM {child_tbl}")
        assert len(rows) == 1
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {child_tbl}")
            await conn.execute_script(f"DROP TABLE IF EXISTS {parent_tbl}")
        except Exception:
            pass


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_composite_target_m2m_migration_lifecycle(db_isolated):
    """Full migration-path lifecycle for a ManyToManyField where BOTH sides have a composite
    PK: CreateModel emits the through-table with one table-level FOREIGN KEY constraint per
    side and the default unique=True's composite UNIQUE index across every column - a real
    orphan reference on either side is rejected by the DB itself. Closes the loop
    test_composite_primary_key.py's generate_schemas()-only M2M DDL tests left open, the other
    DDL hierarchy (migrations)."""
    from tests.testmodels import VersionedArticle, VersionedTag

    conn = db_isolated.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    article_tbl = q(VersionedArticle._meta.db_table, dialect)
    tag_tbl = q(VersionedTag._meta.db_table, dialect)
    field = VersionedArticle._meta.fields_map["tags"]
    through_tbl = q(field.through, dialect)

    # db_isolated already created these tables via generate_schemas() - drop them so
    # create_model() below builds them fresh through the migrations code path this test targets.
    await conn.execute_script(f"DROP TABLE IF EXISTS {through_tbl}")
    await conn.execute_script(f"DROP TABLE IF EXISTS {article_tbl}")
    await conn.execute_script(f"DROP TABLE IF EXISTS {tag_tbl}")
    try:
        await editor.create_model(VersionedTag)
        await editor.create_model(VersionedArticle)

        article_id = "11111111-1111-1111-1111-111111111111"
        tag_id = "22222222-2222-2222-2222-222222222222"
        await conn.execute_script(
            f'INSERT INTO {article_tbl} ("id", "version", "title") VALUES (\'{article_id}\', 1, \'t1\')'
        )
        await conn.execute_script(f'INSERT INTO {tag_tbl} ("id", "version", "name") VALUES (\'{tag_id}\', 1, \'a\')')
        await conn.execute_script(
            f'INSERT INTO {through_tbl} ("versionedarticle_id", "versionedarticle_version", '
            f'"versionedtag_id", "versionedtag_version") '
            f"VALUES ('{article_id}', 1, '{tag_id}', 1)"
        )

        with pytest.raises(Exception):
            await conn.execute_script(
                f'INSERT INTO {through_tbl} ("versionedarticle_id", "versionedarticle_version", '
                f'"versionedtag_id", "versionedtag_version") '
                f"VALUES ('{article_id}', 1, '33333333-3333-3333-3333-333333333333', 1)"
            )

        with pytest.raises(Exception):
            await conn.execute_script(
                f'INSERT INTO {through_tbl} ("versionedarticle_id", "versionedarticle_version", '
                f'"versionedtag_id", "versionedtag_version") '
                f"VALUES ('{article_id}', 1, '{tag_id}', 1)"
            )

        rows = await conn.execute_dicts(f"SELECT COUNT(*) AS row_count FROM {through_tbl}")
        assert rows[0]["row_count"] == 1
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {through_tbl}")
            await conn.execute_script(f"DROP TABLE IF EXISTS {article_tbl}")
            await conn.execute_script(f"DROP TABLE IF EXISTS {tag_tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_pk_field_end_to_end_then_autodetector_reports_no_changes(db_simple):
    """True end-to-end regression check for RenameField.state_forward()'s Meta-level
    bookkeeping fix: generate + apply a migration that renames the PRIMARY KEY field against a
    real database, then rebuild the "current" state the way a fresh makemigrations run would
    (from the already-renamed live model) and confirm the autodetector reports zero further
    changes. Before the fix, StateModelDiff.generate_operations() compared the projected state's
    stale old-name pk_field_name against the live model's new one and raised ConfigurationError
    on every single subsequent makemigrations run, forever, once a PK had ever been renamed."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    tbl = q("test_widget_pk_rename", dialect)

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True)), ("name", CharField(max_length=50))],
        options={"table": "test_widget_pk_rename"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('id', dialect)}, {q('name', dialect)}) VALUES (1, 'gizmo')")

        rename_op = RenameField(model_name="Widget", old_name="id", new_name="pk_id")
        await rename_op.run("models", state, dry_run=False, state_editor=editor)

        # The column rename actually happened at the DB level and the row survived it.
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 1
        assert rows[0]["pk_id"] == 1
        assert rows[0]["name"] == "gizmo"

        # The projected state's PK bookkeeping must have followed the rename.
        projected_model_state = state.models[("models", "Widget")]
        assert projected_model_state.pk_field_name == "pk_id"
        assert projected_model_state.options["pk_attr"] == "pk_id"

        # A fresh "current" state, exactly as makemigrations would build it from the live
        # (already renamed) model on a second, unrelated run.
        RenamedWidget = type(
            "Widget",
            (Model,),
            {
                "pk_id": IntField(primary_key=True),
                "name": CharField(max_length=50),
                "Meta": type("Meta", (), {"table": "test_widget_pk_rename"}),
                "_no_comments": True,
            },
        )
        current_state = State(models={}, apps=StateApps())
        current_state.models[("models", "Widget")] = ModelState.make_from_model("models", RenamedWidget)

        operations = OperationGenerator(state, current_state).generate()
        assert operations == []
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rename_field_with_matching_source_field_keeps_the_db_column(db_simple):
    """RenameField carries the new field's real source_field via field= now - when it explicitly
    names the OLD db column (the field is only being relabeled in Python, not actually moved),
    the physical column must stay put instead of being unconditionally RENAME COLUMN'd. Before the
    fix, RenameField had no field= argument at all: state_forward() just moved the OLD field
    object under the new key, so the new field's own source_field never reached tracked state and
    the column was always renamed regardless of intent."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    tbl = q("test_widget_rename_keep_column", dialect)

    create_op = CreateModel(
        name="Widget",
        fields=[("id", IntField(primary_key=True)), ("old_name", CharField(max_length=32))],
        options={"table": "test_widget_rename_keep_column"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('id', dialect)}, {q('old_name', dialect)}) VALUES (1, 'x')")

        new_field = CharField(max_length=32, source_field="old_name")
        rename_op = RenameField(model_name="Widget", old_name="old_name", new_name="new_name", field=new_field)
        await rename_op.run("models", state, dry_run=False, state_editor=editor)

        # The DB column must still be "old_name" - only the Python-level field name changed.
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 1
        assert rows[0]["old_name"] == "x"
        assert "new_name" not in rows[0]

        # A second makemigrations run against the already-renamed live model must report no
        # further changes - if RenameField's own state_forward() didn't record the real
        # source_field, this would propose yet another operation to "fix" the self-inflicted drift.
        RenamedWidget = type(
            "Widget",
            (Model,),
            {
                "id": IntField(primary_key=True),
                "new_name": CharField(max_length=32, source_field="old_name"),
                "Meta": type("Meta", (), {"table": "test_widget_rename_keep_column"}),
                "_no_comments": True,
            },
        )
        current_state = State(models={}, apps=StateApps())
        current_state.models[("models", "Widget")] = ModelState.make_from_model("models", RenamedWidget)

        operations = OperationGenerator(state, current_state).generate()
        assert operations == []
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_autodetect_and_apply_field_rename_combined_with_type_change_preserves_data(db_simple):
    """A field renamed AND retyped in the same model edit (with an explicit source_field=
    naming the old column) must autodetect as a RenameField + AlterField pair, not fall through
    to a data-losing AddField + RemoveField - the state-diff rename heuristic used to require the
    full field signature (type included) to match on both sides before ever consulting
    source_field=, so a combined rename+retype could never satisfy it and always looked like an
    unrelated add+remove. Builds the old/new ModelStates directly from live model classes (as a
    real makemigrations run would), generates operations from scratch, and applies them against a
    real table with a real pre-existing row."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    tbl = q("test_widget_rename_retype", dialect)

    OldWidget = type(
        "Widget",
        (Model,),
        {
            "id": IntField(primary_key=True),
            "title": CharField(max_length=100),
            "Meta": type("Meta", (), {"table": "test_widget_rename_retype", "app": "models"}),
            "_no_comments": True,
        },
    )
    NewWidget = type(
        "Widget",
        (Model,),
        {
            "id": IntField(primary_key=True),
            "name": TextField(source_field="title"),
            "Meta": type("Meta", (), {"table": "test_widget_rename_retype", "app": "models"}),
            "_no_comments": True,
        },
    )

    old_state = State(models={}, apps=StateApps())
    old_state.models[("models", "Widget")] = ModelState.make_from_model("models", OldWidget)
    new_state = State(models={}, apps=StateApps())
    new_state.models[("models", "Widget")] = ModelState.make_from_model("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 2
    assert isinstance(operations[0], RenameField)
    assert operations[0].old_name == "title"
    assert operations[0].new_name == "name"
    assert isinstance(operations[1], AlterField)
    assert operations[1].name == "name"

    apply_state = State(models={}, apps=StateApps())
    try:
        await CreateModel(
            name="Widget",
            fields=[("id", IntField(primary_key=True)), ("title", CharField(max_length=100))],
            options={"table": "test_widget_rename_retype"},
        ).run("models", apply_state, dry_run=False, state_editor=editor)
        await conn.execute_script(
            f"INSERT INTO {tbl} ({q('id', dialect)}, {q('title', dialect)}) VALUES (1, 'hello world')"
        )

        for operation in operations:
            await operation.run("models", apply_state, dry_run=False, state_editor=editor)

        # The row's data must have survived both the rename and the type change - not been
        # dropped and recreated empty under the new name.
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert len(rows) == 1
        assert rows[0]["title"] == "hello world"
        assert "name" not in rows[0]

        projected_model_state = apply_state.models[("models", "Widget")]
        assert "name" in projected_model_state.fields
        assert "title" not in projected_model_state.fields

        # A fresh "current" state, exactly as makemigrations would build it from the live
        # (already renamed+retyped) model on a second, unrelated run, must report no further
        # changes.
        operations_again = OperationGenerator(apply_state, new_state).generate()
        assert operations_again == []
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_autodetect_and_apply_m2m_field_rename_preserves_through_table(db_isolated):
    """A ManyToManyField owns no column of its own to key a rename off (unlike every other field
    type, it has no source_field), so the generic rename heuristic always missed it - renaming
    just the Python attribute (`tags` -> `labels`, same `through`/`forward_key`/`backward_key`)
    used to autodetect as a plain AddField(labels) + RemoveField(tags) pair instead of a
    RenameField. Applied for real, that RemoveField DROPS the through table and AddField
    re-CREATES an empty one under the identical name (through is derived purely from the two
    owning tables' names, unaffected by the rename) - and in practice never even got that far:
    AddField's own state_forward() re-derives both fields' default related_name while `tags` and
    `labels` still coexist in tracked state, colliding and raising ConfigurationError before any
    DDL ran at all. This is the true end-to-end version of that crash, executed against a real
    through table with real data.

    A ManyToManyField needs Apps._init_relations() to resolve related_model/forward_keys/
    backward_keys (unlike every other field type exercised elsewhere in this file, which works
    fine off bare type()-built classes) - built off db_isolated's own already-registered
    `tests.testmodels.Event.participants` field instead of a hand-rolled model, then the "new"
    (renamed) state is a plain dict-key rename of a clone of the SAME real, fully-resolved field
    - no second field object to independently (and fallibly) reconstruct. Data is seeded through
    the real ORM (Event.objects.create()/.participants.add()) rather than raw per-dialect SQL."""
    from tests.testmodels import Event, Team, Tournament

    conn = db_isolated.db()
    editor = _get_schema_editor(conn)
    field = Event._meta.fields_map["participants"]
    through_tbl = q(field.through, conn.dialect.name)

    old_event_state = ModelState.make_from_model("models", Event)
    old_state = State(models={}, apps=StateApps())
    old_state.models[("models", "Event")] = old_event_state
    old_state.models[("models", "Team")] = ModelState.make_from_model("models", Team)

    new_event_state = old_event_state.clone()
    new_event_state.fields["squads"] = new_event_state.fields.pop("participants")
    new_state = State(models={}, apps=StateApps())
    new_state.models[("models", "Event")] = new_event_state
    new_state.models[("models", "Team")] = ModelState.make_from_model("models", Team)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], RenameField)
    assert operations[0].old_name == "participants"
    assert operations[0].new_name == "squads"

    tournament = await Tournament.objects.create(name="T")
    event = await Event.objects.create(name="E", tournament=tournament)
    team = await Team.objects.create(name="Real Madrid")
    await event.participants.add(team)

    apply_state = State(models={}, apps=StateApps())
    apply_state.models[("models", "Event")] = old_event_state.clone()
    apply_state.models[("models", "Team")] = ModelState.make_from_model("models", Team)

    for operation in operations:
        # This must not raise - applying the pre-fix AddField+RemoveField pair crashed here with
        # ConfigurationError on a duplicate default related_name, before any DDL ran.
        await operation.run("models", apply_state, dry_run=False, state_editor=editor)

    # The through table must still be the SAME table with its data intact - not dropped and
    # recreated empty.
    rows = await conn.execute_dicts(f"SELECT * FROM {through_tbl}")
    assert len(rows) == 1

    projected_event_state = apply_state.models[("models", "Event")]
    assert "squads" in projected_event_state.fields
    assert "participants" not in projected_event_state.fields

    # A fresh "current" state, exactly as makemigrations would build it from the live (already
    # renamed) model on a second, unrelated run, must report no further changes.
    operations_again = OperationGenerator(apply_state, new_state).generate()
    assert operations_again == []


@pytest.mark.asyncio
async def test_alter_field_null_to_not_null_backfills_existing_null_rows_on_every_dialect(db_simple):
    """SQLite's own _remake_table() backfills existing NULL rows via COALESCE when tightening
    null=True -> null=False with a default/db_default (see test_sqlite_remake_table_coalesces_
    null_with_db_default above) - but the shared Postgres/generic ALTER path went straight to
    `ALTER COLUMN ... SET NOT NULL` with no backfill at all, crashing with a raw
    NotNullViolationError for the identical AlterField that succeeded on SQLite."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("note", CharField(max_length=50, null=True)),
        ],
        options={"table": "test_widget_notnull_backfill"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        tbl = q("test_widget_notnull_backfill", dialect)

        await conn.execute_script(f"INSERT INTO {tbl} ({q('note', dialect)}) VALUES (NULL)")

        alter_op = AlterField(
            model_name="Widget",
            name="note",
            field=CharField(max_length=50, null=False, default="fallback"),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('note', dialect)} FROM {tbl}")
        assert rows[0]["note"] == "fallback"
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_widget_notnull_backfill', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_sqlite_remake_table_preserves_surviving_columns_comments(db_simple):
    """SQLite has no ALTER TABLE for a column comment - it only ever lives inline in the CREATE
    TABLE text, so any _remake_table() call (AddConstraint(CheckConstraint) here) rebuilds the
    whole table from scratch. Every _build_remake_field_definitions() branch used to hardcode
    comment="" regardless of what a surviving column actually declares, so an unrelated
    AddConstraint permanently dropped every OTHER column's own comment as a side effect."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect != "sqlite":
        pytest.skip("Column comments only live inline in SQLite's own CREATE TABLE text")

    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Product",
        fields=[
            ("id", IntField(primary_key=True)),
            ("price", IntField(null=False, description="the product's price")),
        ],
        options={"table": "test_product_remake_comment"},
    )
    state = State(models={}, apps=StateApps())

    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)

        constraint = CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_product_remake_comment_price_positive")
        add_op = AddConstraint(model_name="Product", constraint=constraint)
        await add_op.run("models", state, dry_run=False, state_editor=editor)

        _, rows = await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ["test_product_remake_comment"],
        )
        table_sql = dict(rows[0])["sql"]
        assert "the product's price" in table_sql, f"price column comment lost on rebuild: {table_sql!r}"
    finally:
        try:
            await conn.execute_script(f"DROP TABLE IF EXISTS {q('test_product_remake_comment', dialect)}")
        except Exception:
            pass


@pytest.mark.asyncio
async def test_create_model_semicolon_in_description_and_db_default(db_simple):
    """A ';' inside a column comment or a string db_default must not split the atomic DDL."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    tbl = q("test_semicolon_note", dialect)
    create_op = CreateModel(
        name="SemicolonNote",
        fields=[
            ("id", IntField(primary_key=True)),
            ("status", TextField(description="status; one of a, b")),
            ("type", CharField(max_length=20, db_default="a;b")),
            ("quoted", CharField(max_length=20, db_default="it's; -- /* x")),
        ],
        options={"table": "test_semicolon_note"},
    )
    state = State(models={}, apps=StateApps())
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('status', dialect)}) VALUES ('x')")
        rows = await conn.execute_dicts(f"SELECT * FROM {tbl}")
        assert rows[0]["type"] == "a;b"
        assert rows[0]["quoted"] == "it's; -- /* x"
    finally:
        await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")


@pytest.mark.asyncio
async def test_alter_field_narrowing_max_length_raises_instead_of_truncating(db_simple):
    """AlterField narrowing CharField.max_length used to reach Postgres's ALTER COLUMN TYPE via
    an explicit `::varchar(n)` cast, which silently TRUNCATES a too-long existing value instead
    of raising (unlike ordinary parameter-bound INSERT/UPDATE, which would reject it) - confirmed
    live: a 26-character value became `'ABCDE'` with no error at all when narrowed to
    max_length=5. The schema editor must now refuse this with a clear error instead."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("note", CharField(max_length=100)),
        ],
        options={"table": "test_widget_narrow_max_length"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_widget_narrow_max_length", dialect)
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('note', dialect)}) VALUES ('ABCDEFGHIJKLMNOPQRSTUVWXYZ')")

        alter_op = AlterField(model_name="Widget", name="note", field=CharField(max_length=5))
        with pytest.raises(FieldNarrowingDataLossError, match="note"):
            await alter_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('note', dialect)} FROM {tbl}")
        assert rows[0]["note"] == "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    finally:
        await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")


@pytest.mark.asyncio
async def test_alter_field_narrowing_max_length_succeeds_when_no_row_overflows(db_simple):
    """The narrowing guard must only block rows that actually overflow the new max_length - a
    narrower max_length that every existing value already fits within should ALTER normally."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("note", CharField(max_length=100)),
        ],
        options={"table": "test_widget_narrow_max_length_ok"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_widget_narrow_max_length_ok", dialect)
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('note', dialect)}) VALUES ('short')")

        alter_op = AlterField(model_name="Widget", name="note", field=CharField(max_length=5))
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('note', dialect)} FROM {tbl}")
        assert rows[0]["note"] == "short"
    finally:
        await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")


@pytest.mark.asyncio
async def test_alter_field_narrowing_decimal_places_raises_instead_of_rounding(db_simple):
    """Same silent-narrowing risk as max_length, but for DecimalField.max_digits/decimal_places -
    an explicit `::numeric(p,s)` cast on Postgres rounds away extra decimal places (and can
    outright overflow the new max_digits) with no error, unlike ordinary INSERT/UPDATE."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("amount", DecimalField(max_digits=10, decimal_places=5)),
        ],
        options={"table": "test_widget_narrow_decimal"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_widget_narrow_decimal", dialect)
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('amount', dialect)}) VALUES (12.34567)")

        alter_op = AlterField(model_name="Widget", name="amount", field=DecimalField(max_digits=10, decimal_places=2))
        with pytest.raises(FieldNarrowingDataLossError, match="amount"):
            await alter_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('amount', dialect)} FROM {tbl}")
        assert str(rows[0]["amount"]) == "12.34567"
    finally:
        await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")


NARROWING_CASES = [
    ("text_to_char", TextField(), "'ABCDEFGHIJ'", CharField(max_length=5)),
    ("int_to_char", IntField(), "123456", CharField(max_length=3)),
    ("bool_to_char", BooleanField(), "TRUE", CharField(max_length=3)),
    (
        "decimal_whole_digits",
        DecimalField(max_digits=10, decimal_places=2),
        "12345678.12",
        DecimalField(max_digits=10, decimal_places=5),
    ),
    ("float_to_decimal", FloatField(), "1.23456", DecimalField(max_digits=6, decimal_places=2)),
    ("int_to_decimal", IntField(), "123456", DecimalField(max_digits=5, decimal_places=2)),
    ("bigint_to_smallint", BigIntField(), "100000", SmallIntField()),
    ("float_to_int", FloatField(), "1.5", IntField()),
    ("decimal_to_int", DecimalField(max_digits=10, decimal_places=2), "3.25", IntField()),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "old_field", "stored_sql", "new_field"), NARROWING_CASES, ids=[case[0] for case in NARROWING_CASES]
)
async def test_alter_field_refuses_values_the_new_type_cannot_hold(db_simple, label, old_field, stored_sql, new_field):
    """Every dialect refuses an AlterField whose new column type can't hold an existing value -
    Postgres would truncate or round it in the cast, SQLite would keep it beyond the declared size."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    table_name = f"test_narrowing_{label}"
    state = State(models={}, apps=StateApps())
    tbl = q(table_name, dialect)
    try:
        await CreateModel(
            name="Widget",
            fields=[("id", IntField(primary_key=True)), ("value", old_field)],
            options={"table": table_name},
        ).run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('value', dialect)}) VALUES ({stored_sql})")

        with pytest.raises(FieldNarrowingDataLossError, match="value"):
            await AlterField(model_name="Widget", name="value", field=new_field).run(
                "models", state, dry_run=False, state_editor=editor
            )
    finally:
        await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")


FITTING_CHANGES = [
    (
        "decimal_trailing_zeros",
        DecimalField(max_digits=10, decimal_places=5),
        "12.50000",
        DecimalField(max_digits=10, decimal_places=2),
    ),
    ("float_whole_to_int", FloatField(), "7", IntField()),
    ("decimal_whole_to_int", DecimalField(max_digits=10, decimal_places=2), "3", IntField()),
    ("int_to_positive", IntField(), "-5", PositiveIntField()),
    ("text_fits_char", TextField(), "'ABC'", CharField(max_length=5)),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "old_field", "stored_sql", "new_field"), FITTING_CHANGES, ids=[case[0] for case in FITTING_CHANGES]
)
async def test_alter_field_narrowing_keeps_values_that_fit(db_simple, label, old_field, stored_sql, new_field):
    """A narrower type every existing value fits in alters normally - a PositiveIntField has the
    same INT column as an IntField, so a negative value doesn't block it."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    editor = _get_schema_editor(conn)
    table_name = f"test_narrowing_fits_{label}"
    state = State(models={}, apps=StateApps())
    tbl = q(table_name, dialect)
    try:
        await CreateModel(
            name="Widget",
            fields=[("id", IntField(primary_key=True)), ("value", old_field)],
            options={"table": table_name},
        ).run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('value', dialect)}) VALUES ({stored_sql})")

        await AlterField(model_name="Widget", name="value", field=new_field).run(
            "models", state, dry_run=False, state_editor=editor
        )

        rows = await conn.execute_dicts(f"SELECT {q('value', dialect)} FROM {tbl}")
        assert len(rows) == 1
    finally:
        await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")


@pytest.mark.asyncio
async def test_alter_field_type_and_null_tightening_together_applies_type_change_first(db_simple):
    """AlterField changing BOTH the field's type AND null=True->False in the same call used to
    run the null-tightening backfill UPDATE (formatted for the NEW field's type) before the
    deferred, batched ALTER COLUMN TYPE had actually executed - the column was still the OLD
    type at that point. Confirmed live: `IntField(null=True)` with [NULL, 7] altered to
    `CharField(max_length=10, null=False, default="empty")` crashed with
    `invalid input syntax for type integer: "empty"` on both Postgres drivers. The type change
    must now run before the null backfill, so both values land correctly."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect == "sqlite":
        pytest.skip("sqlite goes through its own _remake_table(), not this shared Postgres path")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("count", IntField(null=True)),
        ],
        options={"table": "test_widget_type_and_null_tighten"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_widget_type_and_null_tighten", dialect)
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('count', dialect)}) VALUES (NULL)")
        await conn.execute_script(f"INSERT INTO {tbl} ({q('count', dialect)}) VALUES (7)")

        alter_op = AlterField(
            model_name="Widget",
            name="count",
            field=CharField(max_length=10, null=False, default="empty"),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('count', dialect)} FROM {tbl} ORDER BY {q('id', dialect)}")
        assert {row["count"] for row in rows} == {"empty", "7"}
    finally:
        await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")


@pytest.mark.asyncio
async def test_alter_field_null_and_default_change_without_type_change_still_batches_one_statement(db_simple):
    """Regression guard for the type-change reordering above: an AlterField that does NOT change
    the field's type (only null/default here) must still emit its null-tightening backfill and
    its batched SET NOT NULL/SET DEFAULT changes exactly as before - not split differently just
    because the type-change branch now runs its own ALTER immediately in the type-changing case."""
    conn = db_simple.db()
    dialect = DatabaseUnderTest.get_engine_name(conn.dialect)
    if dialect == "sqlite":
        pytest.skip("sqlite goes through its own _remake_table(), not this shared Postgres path")
    editor = _get_schema_editor(conn)

    create_op = CreateModel(
        name="Widget",
        fields=[
            ("id", IntField(primary_key=True)),
            ("note", CharField(max_length=50, null=True)),
        ],
        options={"table": "test_widget_null_default_only"},
    )
    state = State(models={}, apps=StateApps())
    tbl = q("test_widget_null_default_only", dialect)
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await conn.execute_script(f"INSERT INTO {tbl} ({q('note', dialect)}) VALUES (NULL)")

        alter_op = AlterField(
            model_name="Widget",
            name="note",
            field=CharField(max_length=50, null=False, default="fallback"),
        )
        await alter_op.run("models", state, dry_run=False, state_editor=editor)

        rows = await conn.execute_dicts(f"SELECT {q('note', dialect)} FROM {tbl}")
        assert rows[0]["note"] == "fallback"
    finally:
        await conn.execute_script(f"DROP TABLE IF EXISTS {tbl}")
