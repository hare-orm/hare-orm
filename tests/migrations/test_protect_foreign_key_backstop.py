"""on_delete=PROTECT's database-level backstop: a NO ACTION DEFERRABLE INITIALLY IMMEDIATE (deferred
only by a hard delete, for its own DELETE) - and AlterField rebuilding an M2M through table's
constraints on an on_delete change."""

from __future__ import annotations

import re
from copy import copy
from pathlib import Path

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import IntegrityError
from hare.fields import CharField
from hare.fields.constants import CASCADE, SET_NULL
from hare.migrations.execution.executor import MigrationExecutor
from hare.migrations.operations import AlterField
from hare.migrations.state.project import ModelState, State, StateApps
from tests.migrations.test_operations_real_db import _get_schema_editor
from tests.testmodels import (
    Dest_null,
    M2MOnDeleteProtectParent,
    M2MOnDeleteProtectPeer,
    O2O_null,
)

CURRENT_PROTECT_RULE = ("NO ACTION", "IMMEDIATE")


async def get_foreign_key_rule(connection, table: str, column: str) -> tuple[str, str]:
    """Returns the real (delete rule, check timing) of the FK constraint on ``table.column`` - the
    timing is "NOT DEFERRABLE", "IMMEDIATE" (deferrable, initially immediate) or "DEFERRED"."""
    if connection.dialect.name == "sqlite":
        rows = await connection.execute_dicts(f'PRAGMA foreign_key_list("{table}")')
        (rule,) = [row["on_delete"] for row in rows if row["from"] == column]
        table_sql_rows = await connection.execute_dicts(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", [table]
        )
        # The column's own inline "REFERENCES ..." clause runs up to the next column definition.
        column_clause = re.search(
            rf'"{re.escape(column)}" [^"]*?REFERENCES "[^"]+" \([^)]*\)[^,]*', table_sql_rows[0]["sql"]
        )
        assert column_clause is not None
        timing = re.search(r"DEFERRABLE INITIALLY (\w+)", column_clause.group(0))
        return rule, timing.group(1).upper() if timing else "NOT DEFERRABLE"
    rows = await connection.execute_dicts(
        "SELECT con.confdeltype::text AS rule, con.condeferrable AS deferrable, con.condeferred AS deferred "
        "FROM pg_constraint con "
        "JOIN pg_class rel ON rel.oid = con.conrelid "
        "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey) "
        "WHERE rel.relname = $1 AND att.attname = $2 AND con.contype = 'f'",
        [table, column],
    )
    (row,) = rows
    rule_names = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
    if not row["deferrable"]:
        return rule_names[row["rule"]], "NOT DEFERRABLE"
    return rule_names[row["rule"]], "DEFERRED" if row["deferred"] else "IMMEDIATE"


def build_state(*models) -> State:
    state = State(models={}, apps=StateApps())
    keys = []
    for model in models:
        key = ("models", model.__name__)
        state.models[key] = ModelState.make_from_model("models", model)
        keys.append(key)
    state.reload_models(keys)
    return state


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_alter_m2m_on_delete_rebuilds_the_through_table_constraints(db_isolated):
    """Bug: AlterField changing a ManyToManyField's on_delete only renamed the through table/
    columns and never touched its FK constraints, so the database kept the old ON DELETE action."""
    connection = db_isolated.db()
    editor = _get_schema_editor(connection)
    protect_field = M2MOnDeleteProtectParent._meta.fields_map["peers"]
    through_table = protect_field.through
    backward_key = protect_field.backward_keys[0]
    forward_key = protect_field.forward_keys[0]

    cascade_field = copy(protect_field)
    cascade_field.on_delete = CASCADE
    await editor._alter_m2m_field(M2MOnDeleteProtectParent, protect_field, cascade_field)
    assert (await get_foreign_key_rule(connection, through_table, backward_key))[0] == "CASCADE"
    assert (await get_foreign_key_rule(connection, through_table, forward_key))[0] == "CASCADE"

    parent = await M2MOnDeleteProtectParent.objects.create(name="parent")
    peer = await M2MOnDeleteProtectPeer.objects.create(name="peer")
    await parent.peers.add(peer)
    parent_table = M2MOnDeleteProtectParent._meta.db_table
    await connection.execute(f'DELETE FROM "{parent_table}" WHERE "id" = {parent.pk}')
    assert await connection.execute_dicts(f'SELECT * FROM "{through_table}"') == []

    set_null_field = copy(protect_field)
    set_null_field.on_delete = SET_NULL
    await editor._alter_m2m_field(M2MOnDeleteProtectParent, cascade_field, set_null_field)
    assert (await get_foreign_key_rule(connection, through_table, backward_key))[0] == "SET NULL"

    await editor._alter_m2m_field(M2MOnDeleteProtectParent, set_null_field, protect_field)
    assert await get_foreign_key_rule(connection, through_table, backward_key) == CURRENT_PROTECT_RULE
    assert await get_foreign_key_rule(connection, through_table, forward_key) == CURRENT_PROTECT_RULE


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_table_rebuild_keeps_the_unique_constraint_of_a_one_to_one_field(db_isolated):
    """Bug: SQLite's table rebuild (any AlterField of another column) re-created a OneToOneField's
    column without UNIQUE, so two rows could point at one target."""
    connection = db_isolated.db()
    editor = _get_schema_editor(connection)
    state = build_state(O2O_null, Dest_null)

    await AlterField(model_name="O2O_null", name="name", field=CharField(max_length=128)).run(
        "models", state, dry_run=False, state_editor=editor
    )

    target = await Dest_null.objects.create(name="target")
    await O2O_null.objects.create(name="first", event=target)
    with pytest.raises(IntegrityError):
        await O2O_null.objects.create(name="second", event=target)


def write_protect_migrations(tmp_path: Path, app_label: str, schema_operations: list[str]) -> str:
    """Writes an app whose 0001 creates a parent table and a child with a PROTECT FK to it, and whose
    atomic 0002 inserts a row into each, then runs ``schema_operations``."""
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "0001_initial.py").write_text(
        "\n".join(
            [
                "from hare import fields, migrations",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                "    dependencies = []",
                "    operations = [",
                "        ops.CreateModel(",
                "            name='Parent',",
                "            fields=[('id', fields.IntField(primary_key=True))],",
                f"            options={{'table': '{app_label}_parent'}},",
                "        ),",
                "        ops.CreateModel(",
                "            name='Child',",
                "            fields=[",
                "                ('id', fields.IntField(primary_key=True)),",
                f"                ('parent', fields.ForeignKeyField('{app_label}.Parent', on_delete=fields.PROTECT)),",
                "            ],",
                f"            options={{'table': '{app_label}_child'}},",
                "        ),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )
    (migrations_dir / "0002_data_then_schema.py").write_text(
        "\n".join(
            [
                "from hare import fields, migrations",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                f"    dependencies = [({app_label!r}, '0001_initial')]",
                "    atomic = True",
                "    operations = [",
                f'        ops.RunSQL(\'INSERT INTO "{app_label}_parent" ("id") VALUES (1)\'),',
                f'        ops.RunSQL(\'INSERT INTO "{app_label}_child" ("id", "parent_id") VALUES (1, 1)\'),',
                *[f"        {operation}," for operation in schema_operations],
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )
    return f"{app_label}.migrations"


@pytest.mark.asyncio
async def test_atomic_migration_alters_a_protect_child_table_after_writing_to_it(
    db_isolated_no_schema, tmp_path, monkeypatch
):
    """Bug: with an INITIALLY DEFERRED backstop, a write to a table with a PROTECT FK left pending
    trigger events, and a later ALTER TABLE of that table in the same (atomic migration)
    transaction failed on Postgres with "cannot ALTER TABLE ... because it has pending trigger
    events"."""
    app_label = "protectdataschemaapp"
    migrations_module = write_protect_migrations(
        tmp_path, app_label, ["ops.AddField(model_name='Child', name='note', field=fields.TextField(null=True))"]
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    connection = db_isolated_no_schema.db()
    apps_config = {
        app_label: {"models": [], "default_connection": connection.connection_name, "migrations": migrations_module}
    }

    await MigrationExecutor(connection, apps_config).migrate()

    rows = await connection.execute_dicts(f'SELECT "id", "parent_id", "note" FROM "{app_label}_child"')
    assert [dict(row) for row in rows] == [{"id": 1, "parent_id": 1, "note": None}]


@pytest.mark.asyncio
async def test_atomic_migration_truncates_a_protect_child_table_after_writing_to_it(
    db_isolated_no_schema, tmp_path, monkeypatch
):
    connection = db_isolated_no_schema.db()
    if connection.dialect.name != "postgresql":
        pytest.skip("TRUNCATE is Postgres-only")
    app_label = "protecttruncateapp"
    migrations_module = write_protect_migrations(
        tmp_path, app_label, [f"ops.RunSQL('TRUNCATE \"{app_label}_child\"')"]
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    apps_config = {
        app_label: {"models": [], "default_connection": connection.connection_name, "migrations": migrations_module}
    }

    await MigrationExecutor(connection, apps_config).migrate()

    assert await connection.execute_dicts(f'SELECT * FROM "{app_label}_child"') == []
