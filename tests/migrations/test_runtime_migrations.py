from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import cast

import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.core.hare_context import HareContext
from hare.dialects.base.client import DatabaseClient
from hare.dialects.base.features import Features
from hare.dialects.base.results import StatementResult
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.dialects.sqlite.query import SqliteQuery
from hare.exceptions import IntegrityError
from hare.migrations.exceptions import IrreversibleMigrationError, MigrationLoadError, PartiallyAppliedMigrationError
from hare.migrations.execution.executor import MigrationExecutor, MigrationTarget
from hare.migrations.loading.graph import MigrationGraph, MigrationKey
from hare.migrations.loading.migration_loader import MigrationLoader
from hare.migrations.loading.recorder import MigrationRecorder
from hare.migrations.migration import Migration
from hare.migrations.operations import HareOperation
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models import Model


class FakeConnection:
    def __init__(self, *, applied: list[MigrationKey] | None = None) -> None:
        self.dialect = SQLITE_DIALECT
        self.query_class = SqliteQuery
        self.features = Features(inline_comments=True)
        self.connection_alias = "default"
        self._applied = applied or []
        self.executed_scripts: list[str] = []
        self.inserts: list[tuple[str, list]] = []
        self.queries: list[tuple[str, list | None]] = []

    async def execute(self, query: str, values: list | None = None, *, returns_rows: bool | None = None):
        if query.lstrip().upper().startswith("INSERT"):
            self.inserts.append((query, values))
            return StatementResult(1, [])
        self.queries.append((query, values))
        rows = [{"app": key.app_label, "name": key.name} for key in self._applied]
        return StatementResult(0, rows)

    async def execute_script(self, query: str) -> None:
        self.executed_scripts.append(query)


def _write_migrations(tmp_path: Path, app_label: str, migrations: list[tuple[str, list[tuple[str, str]]]]) -> str:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    for name, dependencies in migrations:
        content = [
            "from hare import migrations",
            "",
            "class Migration(migrations.Migration):",
            f"    dependencies = {dependencies!r}",
            "",
            "    operations = []",
            "",
        ]
        (migrations_dir / f"{name}.py").write_text("\n".join(content), encoding="ascii")
    return f"{app_label}.migrations"


def _write_runpython_migrations(tmp_path: Path, app_label: str) -> str:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")

    (migrations_dir / "0001_initial.py").write_text(
        "\n".join(
            [
                "from hare import migrations",
                "from hare import fields",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                "    dependencies = []",
                "",
                "    operations = [",
                "        ops.CreateModel(",
                "            name='Post',",
                "            fields=[",
                "                ('id', fields.IntField(primary_key=True)),",
                "                ('title', fields.CharField(max_length=200)),",
                "                ('summary', fields.TextField(null=True)),",
                "            ],",
                "        ),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )

    (migrations_dir / "0002_runpython.py").write_text(
        "\n".join(
            [
                "from hare import migrations",
                "from hare.query.expressions import F",
                "from hare.migrations import operations as ops",
                "",
                "CALLS = []",
                "",
                "",
                "async def populate_summary(apps, schema_editor) -> None:",
                "    CALLS.append('forward')",
                "    Post = apps.get_model('blog.Post')",
                "    await Post.objects.filter(summary=None).update(summary=F('title'))",
                "",
                "",
                "async def reset_summary(apps, schema_editor) -> None:",
                "    CALLS.append('reverse')",
                "    Post = apps.get_model('blog.Post')",
                "    await Post.objects.all().update(summary=None)",
                "",
                "",
                "class Migration(migrations.Migration):",
                "    dependencies = [('blog', '0001_initial')]",
                "",
                "    operations = [",
                "        ops.RunPython(",
                "            code=populate_summary,",
                "            reverse_code=reset_summary,",
                "        ),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )

    (migrations_dir / "0003_rename_excerpt.py").write_text(
        "\n".join(
            [
                "from hare import migrations",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                "    dependencies = [('blog', '0002_runpython')]",
                "",
                "    operations = [",
                "        ops.RenameField(",
                "            model_name='Post',",
                "            old_name='summary',",
                "            new_name='excerpt',",
                "        ),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )

    return f"{app_label}.migrations"


def _write_cascade_migrations(tmp_path: Path, app_label: str) -> str:
    """A parent/child pair where the child has an ON DELETE CASCADE FK to the parent, followed
    by a migration that ALTERs an unrelated field on the parent - triggers SqliteSchemaEditor's
    table-rebuild path (DROP TABLE + CREATE TABLE + RENAME) for a table other tables reference."""
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")

    (migrations_dir / "0001_initial.py").write_text(
        "\n".join(
            [
                "from hare import migrations",
                "from hare import fields",
                "from hare.fields.constants import CASCADE",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                "    dependencies = []",
                "",
                "    operations = [",
                "        ops.CreateModel(",
                "            name='Parent',",
                "            fields=[",
                "                ('id', fields.IntField(primary_key=True)),",
                "                ('name', fields.CharField(max_length=50)),",
                "            ],",
                "        ),",
                "        ops.CreateModel(",
                "            name='Child',",
                "            fields=[",
                "                ('id', fields.IntField(primary_key=True)),",
                "                ('parent', fields.ForeignKeyField(",
                f"                    '{app_label}.Parent', related_name='children', on_delete=CASCADE,",
                "                )),",
                "            ],",
                "        ),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )

    (migrations_dir / "0002_alter_parent.py").write_text(
        "\n".join(
            [
                "from hare import migrations",
                "from hare import fields",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                f"    dependencies = [('{app_label}', '0001_initial')]",
                "",
                "    operations = [",
                "        ops.AlterField(",
                "            model_name='Parent',",
                "            name='name',",
                "            field=fields.CharField(max_length=100),",
                "        ),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )

    return f"{app_label}.migrations"


@pytest.mark.asyncio
async def test_sqlite_table_rebuild_does_not_cascade_delete_referencing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SqliteSchemaEditor._remake_table() drops and recreates a table for most ALTER-adjacent
    operations - with PRAGMA foreign_keys=ON (hare's own default), DROP TABLE on a table
    another table has an ON DELETE CASCADE FK pointed at performs an implicit DELETE of every
    row first, cascading into that other table's own rows. An unrelated AlterField on the
    PARENT here must not delete the CHILD row referencing it."""
    module_path = _write_cascade_migrations(tmp_path, "cascadeapp")
    monkeypatch.syspath_prepend(str(tmp_path))

    async with HareContext() as ctx:
        ctx.connections._init_config(
            {
                "default": {
                    "engine": "sqlite+aiosqlite",
                    "credentials": {"file_path": str(tmp_path / "cascade.sqlite3")},
                }
            }
        )
        apps_config = {"cascadeapp": {"models": [], "default_connection": "default", "migrations": module_path}}
        connection = ctx.connections.get("default")
        executor = MigrationExecutor(connection, apps_config)

        await executor.migrate(targets=[MigrationTarget(app_label="cascadeapp", name="0001_initial")])

        await connection.execute("INSERT INTO parent (id, name) VALUES (1, 'p1')")
        await connection.execute("INSERT INTO child (id, parent_id) VALUES (1, 1)")

        # The unrelated AlterField on Parent triggers the rebuild.
        await executor.migrate()

        _, rows = await connection.execute("SELECT COUNT(*) AS n FROM child")
        assert dict(rows[0])["n"] == 1

        # The rebuild itself must have actually happened (not silently skipped).
        _, parent_schema = await connection.execute("SELECT sql FROM sqlite_master WHERE name = 'parent'")
        assert "VARCHAR(100)" in dict(parent_schema[0])["sql"]

        # PRAGMA foreign_keys must be restored to its configured default afterward, not left
        # disabled for the rest of the connection's lifetime.
        _, fk_rows = await connection.execute("PRAGMA foreign_keys")
        assert dict(fk_rows[0])["foreign_keys"] == 1

        with pytest.raises(Exception, match="FOREIGN KEY constraint failed"):
            await connection.execute("INSERT INTO child (id, parent_id) VALUES (2, 999)")

        await connection.close()


def _write_fk_to_unmanaged_migration(tmp_path: Path, app_label: str) -> str:
    """One CreateModel for 'Widget', with a ForeignKeyField string-referencing
    '<app_label>.ExternalThing' - a model this migration sequence never creates, standing in
    for a Meta.managed=False model the test registers live instead (see the test itself)."""
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")

    (migrations_dir / "0001_initial.py").write_text(
        "\n".join(
            [
                "from hare import migrations",
                "from hare import fields",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                "    dependencies = []",
                "",
                "    operations = [",
                "        ops.CreateModel(",
                "            name='Widget',",
                "            fields=[",
                "                ('id', fields.IntField(primary_key=True)),",
                "                ('external', fields.ForeignKeyField(",
                f"                    '{app_label}.ExternalThing', related_name='widgets',",
                "                )),",
                "            ],",
                "        ),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )
    return f"{app_label}.migrations"


@pytest.mark.asyncio
async def test_migrate_with_fk_to_unmanaged_model_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed model's FK pointing at a Meta.managed = False model must not crash migrate() -
    Meta.managed = False models never get a CreateModel of their own (by design, they're never
    hare-orm's to create), so StateApps.init_relations()'s "wait for a later CreateModel"
    workaround for out-of-order model registration used to wait forever for one that would never
    come, tripping State.validate_relations_initialized()'s safety net with a RuntimeError even
    though the referenced table already genuinely exists."""
    module_path = _write_fk_to_unmanaged_migration(tmp_path, "fkmanagedapp")
    monkeypatch.syspath_prepend(str(tmp_path))

    async with HareContext() as ctx:
        ctx.connections._init_config(
            {
                "default": {
                    "engine": "sqlite+aiosqlite",
                    "credentials": {"file_path": str(tmp_path / "fkmanaged.sqlite3")},
                }
            }
        )
        connection = ctx.connections.get("default")
        # The unmanaged table already exists by some other means (e.g. it's owned by a
        # different service/migration system entirely) - hare never creates it.
        await connection.execute_script('CREATE TABLE "external_thing" ("id" INTEGER NOT NULL PRIMARY KEY)')
        await connection.execute('INSERT INTO "external_thing" ("id") VALUES (1)')

        class ExternalThing(Model):
            id = fields.IntField(primary_key=True)

            class Meta:
                app = "fkmanagedapp"
                table = "external_thing"
                managed = False

        from hare.core.hare import Hare

        Hare.register_live_models([ExternalThing], app_label="fkmanagedapp", connection_alias="default")

        apps_config = {"fkmanagedapp": {"models": [], "default_connection": "default", "migrations": module_path}}
        executor = MigrationExecutor(connection, apps_config)

        await executor.migrate(targets=[MigrationTarget(app_label="fkmanagedapp", name="0001_initial")])

        _, widget_schema = await connection.execute("SELECT sql FROM sqlite_master WHERE name = 'widget'")
        assert "external_id" in dict(widget_schema[0])["sql"]

        await connection.execute('INSERT INTO "widget" ("id", "external_id") VALUES (1, 1)')
        _, rows = await connection.execute('SELECT "external_id" FROM "widget" WHERE "id" = 1')
        assert dict(rows[0])["external_id"] == 1

        await connection.close()


def _write_non_atomic_partial_failure_migration(tmp_path: Path, app_label: str) -> str:
    """One migration, ``atomic = False``, with two operations - CreateModel (succeeds) followed
    by a RunPython that always raises - simulating a non-atomic migration failing partway
    through, used by the PartiallyAppliedMigrationError test below."""
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")

    (migrations_dir / "0001_initial.py").write_text(
        "\n".join(
            [
                "from hare import migrations",
                "from hare import fields",
                "from hare.migrations import operations as ops",
                "",
                "",
                "async def always_fails(apps, schema_editor) -> None:",
                "    raise RuntimeError('simulated failure partway through the migration')",
                "",
                "",
                "class Migration(migrations.Migration):",
                "    dependencies = []",
                "    atomic = False",
                "",
                "    operations = [",
                "        ops.CreateModel(",
                "            name='Widget',",
                "            fields=[",
                "                ('id', fields.IntField(primary_key=True)),",
                "            ],",
                "        ),",
                "        ops.RunPython(code=always_fails),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )
    return f"{app_label}.migrations"


@pytest.mark.asyncio
async def test_non_atomic_migration_failing_partway_raises_partially_applied_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-atomic (atomic = False) migration whose first operation succeeds and second raises
    used to leak the raw RunPython exception straight through migrate() - CreateModel's DDL had
    already landed (no transaction wraps the whole migration), but the migration was never
    recorded as applied, so a plain retry would replay CreateModel too and hit a confusing
    "table already exists" instead of a clear, actionable error up front."""
    module_path = _write_non_atomic_partial_failure_migration(tmp_path, "partialapp")
    monkeypatch.syspath_prepend(str(tmp_path))

    async with HareContext() as ctx:
        ctx.connections._init_config(
            {
                "default": {
                    "engine": "sqlite+aiosqlite",
                    "credentials": {"file_path": str(tmp_path / "partial.sqlite3")},
                }
            }
        )
        apps_config = {"partialapp": {"models": [], "default_connection": "default", "migrations": module_path}}
        connection = ctx.connections.get("default")
        executor = MigrationExecutor(connection, apps_config)

        with pytest.raises(PartiallyAppliedMigrationError) as exc_info:
            await executor.migrate()

        assert "partialapp.0001_initial" in str(exc_info.value)
        assert "simulated failure" in str(exc_info.value)

        # CreateModel's own DDL already landed - proves this genuinely ran non-atomically,
        # not that the whole migration was cleanly rolled back.
        _, widget_schema = await connection.execute("SELECT sql FROM sqlite_master WHERE name = 'widget'")
        assert widget_schema

        # Never recorded as applied - a plain retry would still replay CreateModel from scratch.
        recorder = MigrationRecorder(connection)
        applied = await recorder.applied_migrations()
        assert MigrationKey(app_label="partialapp", name="0001_initial") not in applied

        await connection.close()


@pytest.mark.asyncio
async def test_graph_planning_with_keys() -> None:
    graph = MigrationGraph()
    key1 = MigrationKey(app_label="models", name="0001_initial")
    key2 = MigrationKey(app_label="models", name="0002_second")
    graph.add_node(key1, Migration(key1.name, key1.app_label))
    graph.add_node(key2, Migration(key2.name, key2.app_label))
    graph.add_dependency(key2, key2, key1)

    assert graph.forwards_plan(key2) == [key1, key2]
    assert graph.backwards_plan(key1) == [key2, key1]


@pytest.mark.asyncio
async def test_graph_multi_app_dependencies() -> None:
    graph = MigrationGraph()
    a1 = MigrationKey(app_label="app1", name="0001_initial")
    a2 = MigrationKey(app_label="app1", name="0002_second")
    b1 = MigrationKey(app_label="app2", name="0001_initial")
    graph.add_node(a1, Migration(a1.name, a1.app_label))
    graph.add_node(a2, Migration(a2.name, a2.app_label))
    graph.add_node(b1, Migration(b1.name, b1.app_label))
    graph.add_dependency(a2, a2, a1)
    graph.add_dependency(b1, b1, a2)

    assert graph.forwards_plan(b1) == [a1, a2, b1]


@pytest.mark.asyncio
async def test_loader_builds_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _write_migrations(
        tmp_path,
        "app",
        [
            ("0001_initial", []),
            ("0002_second", [("app", "0001_initial")]),
        ],
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    apps_config = {"app": {"models": [], "default_connection": "default", "migrations": module_path}}

    class FakeRecorder:
        async def applied_migrations(self):
            return []

    loader = MigrationLoader(apps_config, cast(MigrationRecorder, FakeRecorder()), load=False)
    await loader.build_graph()

    key1 = MigrationKey(app_label="app", name="0001_initial")
    key2 = MigrationKey(app_label="app", name="0002_second")
    assert key1 in loader.graph.nodes
    assert key2 in loader.graph.nodes
    assert loader.graph.forwards_plan(key2) == [key1, key2]


@pytest.mark.asyncio
async def test_loader_missing_module_raises(tmp_path: Path) -> None:
    apps_config = {"app": {"models": [], "default_connection": "default", "migrations": "nope.migrations"}}

    class FakeRecorder:
        async def applied_migrations(self):
            return []

    loader = MigrationLoader(apps_config, cast(MigrationRecorder, FakeRecorder()), load=False)
    with pytest.raises(MigrationLoadError, match="nope.migrations"):
        await loader.build_graph()


@pytest.mark.asyncio
async def test_executor_plan_forward_and_backward(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _write_migrations(
        tmp_path,
        "app",
        [
            ("0001_initial", []),
            ("0002_second", [("app", "0001_initial")]),
        ],
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    apps_config = {"app": {"models": [], "default_connection": "default", "migrations": module_path}}
    connection = FakeConnection()
    executor = MigrationExecutor(cast(DatabaseClient, connection), apps_config)

    steps = await executor.plan()
    assert [step.backward for step in steps] == [False, False]
    assert [step.migration.name for step in steps] == ["0001_initial", "0002_second"]

    applied = [
        MigrationKey(app_label="app", name="0001_initial"),
        MigrationKey(app_label="app", name="0002_second"),
    ]
    connection = FakeConnection(applied=applied)
    executor = MigrationExecutor(cast(DatabaseClient, connection), apps_config)
    steps = await executor.plan([MigrationTarget(app_label="app", name="zero")])
    assert [step.backward for step in steps] == [True, True]
    assert [step.migration.name for step in steps] == ["0002_second", "0001_initial"]


@pytest.mark.asyncio
async def test_executor_plan_cross_app_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app1_module = _write_migrations(tmp_path, "app1", [("0001_initial", [])])
    app2_module = _write_migrations(tmp_path, "app2", [("0001_initial", [("app1", "0001_initial")])])
    monkeypatch.syspath_prepend(str(tmp_path))

    apps_config = {
        "app1": {"models": [], "default_connection": "default", "migrations": app1_module},
        "app2": {"models": [], "default_connection": "default", "migrations": app2_module},
    }
    connection = FakeConnection()
    executor = MigrationExecutor(cast(DatabaseClient, connection), apps_config)

    steps = await executor.plan([MigrationTarget(app_label="app2", name="__latest__")])
    assert [step.migration.app_label for step in steps] == ["app1", "app2"]
    assert [step.migration.name for step in steps] == ["0001_initial", "0001_initial"]


@pytest.mark.asyncio
async def test_migration_apply_and_unapply_flags() -> None:
    class MarkerOperation(HareOperation):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def state_forward(self, app_label: str, state: State) -> None:
            self.calls.append("state_forward")

        async def database_forward(self, app_label, old_state, new_state, state_editor):
            self.calls.append("database_forward")

        async def database_backward(self, app_label, old_state, new_state, state_editor):
            self.calls.append("database_backward")

    class TestMigration(Migration):
        operations = [MarkerOperation()]

    migration = TestMigration("0001_initial", "models")
    state = State(models={}, apps=StateApps())

    await migration.apply(state, dry_run=True, schema_editor=None)
    marker = cast(MarkerOperation, migration.operations[0])
    assert marker.calls == ["state_forward"]

    await migration.unapply(state, dry_run=True, schema_editor=None)
    marker = cast(MarkerOperation, migration.operations[0])
    assert marker.calls == ["state_forward", "state_forward"]


@pytest.mark.asyncio
async def test_migration_unapply_requires_reversible() -> None:
    """The error message must name the actual migration and operation - it used to show a raw
    `<...IrreversibleOperation object at 0x...>` instead, since neither Operation nor Migration
    defined __str__."""

    class IrreversibleOperation(HareOperation):
        reversible = False

    class TestMigration(Migration):
        operations = [IrreversibleOperation()]

    migration = TestMigration("0001_initial", "models")
    state = State(models={}, apps=StateApps())

    with pytest.raises(IrreversibleMigrationError) as exc_info:
        await migration.unapply(state, dry_run=True, schema_editor=None)

    message = str(exc_info.value)
    assert "models.0001_initial" in message
    assert "IrreversibleOperation" in message
    assert "0x" not in message


@pytest.mark.asyncio
async def test_recorder_reads_and_writes() -> None:
    applied = [MigrationKey(app_label="app", name="0001_initial")]
    connection = FakeConnection(applied=applied)
    recorder = MigrationRecorder(connection)

    rows = await recorder.applied_migrations()
    assert rows == applied

    await recorder.record_applied("app", "0002_second")
    await recorder.record_unapplied("app", "0001_initial")
    assert any("INSERT INTO" in q for q, _ in connection.inserts)
    assert any("DELETE FROM" in q for q, _ in connection.queries)


@pytest.mark.asyncio
async def test_executor_plan_ordering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = _write_migrations(
        tmp_path,
        "app",
        [
            ("0001_initial", []),
            ("0002_second", [("app", "0001_initial")]),
        ],
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    apps_config = {"app": {"models": [], "default_connection": "default", "migrations": module_path}}
    connection = FakeConnection()
    executor = MigrationExecutor(cast(DatabaseClient, connection), apps_config)
    steps = await executor.plan([MigrationTarget(app_label="app", name="__latest__")])

    assert [step.migration.name for step in steps] == ["0001_initial", "0002_second"]


@pytest.mark.asyncio
async def test_runpython_historical_models_survive_schema_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_path = _write_runpython_migrations(tmp_path, "blog")
    monkeypatch.syspath_prepend(str(tmp_path))

    async with HareContext() as ctx:
        ctx.connections._init_config(
            {
                "default": {
                    "engine": "sqlite+aiosqlite",
                    "credentials": {"file_path": str(tmp_path / "runpython.sqlite3")},
                }
            }
        )
        apps_config = {
            "blog": {
                "models": [],
                "default_connection": "default",
                "migrations": module_path,
            }
        }
        connection = ctx.connections.get("default")
        executor = MigrationExecutor(connection, apps_config)

        await executor.migrate()
        module = importlib.import_module(f"{module_path}.0002_runpython")
        assert module.CALLS == ["forward"]

        await executor.migrate([MigrationTarget(app_label="blog", name="0001_initial")])
        assert module.CALLS == ["forward", "reverse"]

        await executor.migrate([MigrationTarget(app_label="blog", name="__latest__")])
        assert module.CALLS == ["forward", "reverse", "forward"]

        await connection.close()


MIGRATION_SOURCE_HEADER = "from hare import migrations, fields\nfrom hare.migrations import operations as ops\n\n"


def _write_migration_sources(tmp_path: Path, app_label: str, sources: dict[str, str]) -> str:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    for name, body in sources.items():
        (migrations_dir / f"{name}.py").write_text(MIGRATION_SOURCE_HEADER + body, encoding="ascii")
    return f"{app_label}.migrations"


def _sqlite_file_config(path: Path) -> dict:
    return {"default": {"engine": "sqlite+aiosqlite", "credentials": {"file_path": str(path)}}}


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_atomic_operation_inside_non_atomic_migration_runs_in_its_own_transaction(
    db_isolated_no_schema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operation with atomic=True inside an atomic=False migration must run on its own
    transaction's connection: committed as a unit on success, rolled back as a unit on
    failure. On SQLite it used to deadlock, on Postgres it ran outside the transaction."""
    module_path = _write_migration_sources(
        tmp_path,
        "peropatomicapp",
        {
            "0001_initial": (
                "class Migration(migrations.Migration):\n"
                "    operations = [\n"
                "        ops.RunSQL('CREATE TABLE perop_t (id INTEGER PRIMARY KEY)', 'DROP TABLE perop_t'),\n"
                "    ]\n"
            ),
            "0002_committed": (
                "class Migration(migrations.Migration):\n"
                "    atomic = False\n"
                "    dependencies = [('peropatomicapp', '0001_initial')]\n"
                "    operations = [ops.RunSQL(['INSERT INTO perop_t (id) VALUES (1)'], atomic=True)]\n"
            ),
            "0003_rolled_back": (
                "class Migration(migrations.Migration):\n"
                "    atomic = False\n"
                "    dependencies = [('peropatomicapp', '0002_committed')]\n"
                "    operations = [\n"
                "        ops.RunSQL(\n"
                "            ['INSERT INTO perop_t (id) VALUES (2)', 'INSERT INTO perop_missing_t VALUES (1)'],\n"
                "            atomic=True,\n"
                "        ),\n"
                "    ]\n"
            ),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    connection = db_isolated_no_schema.get_connection()
    apps_config = {
        "peropatomicapp": {
            "models": [],
            "default_connection": connection.connection_alias,
            "migrations": module_path,
        }
    }
    executor = MigrationExecutor(connection, apps_config)

    with pytest.raises(PartiallyAppliedMigrationError, match="0003_rolled_back"):
        await asyncio.wait_for(executor.migrate(), timeout=30)

    _, rows = await connection.execute("SELECT id FROM perop_t ORDER BY id")
    assert [dict(row)["id"] for row in rows] == [1]
    applied = await MigrationRecorder(connection).applied_migrations()
    assert [key.name for key in applied] == ["0001_initial", "0002_committed"]


@pytest.mark.asyncio
async def test_migrate_dry_run_does_not_create_the_migrations_table(
    db_isolated_no_schema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_path = _write_migration_sources(
        tmp_path,
        "dryrunapp",
        {
            "0001_initial": (
                "class Migration(migrations.Migration):\n"
                "    operations = [ops.CreateModel(name='DryRunWidget', "
                "fields=[('id', fields.IntField(generated=True, primary_key=True))], "
                "options={'table': 'dry_run_widget'})]\n"
            ),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    connection = db_isolated_no_schema.get_connection()
    apps_config = {
        "dryrunapp": {"models": [], "default_connection": connection.connection_alias, "migrations": module_path}
    }
    executor = MigrationExecutor(connection, apps_config)

    await asyncio.wait_for(executor.migrate(dry_run=True), timeout=30)

    recorder = MigrationRecorder(connection)
    assert await recorder._table_exists() is False
    assert await recorder.applied_migrations() == []


def _foreign_key_violation_sources(app_label: str, *, atomic: bool, operation_atomic: bool | None) -> dict[str, str]:
    return {
        "0001_initial": (
            "class Migration(migrations.Migration):\n"
            "    operations = [\n"
            "        ops.CreateModel(name='Parent', fields=[('id', fields.IntField(primary_key=True))], "
            "options={'table': 'fk_parent'}),\n"
            "        ops.CreateModel(name='Child', fields=[('id', fields.IntField(primary_key=True)), "
            f"('parent', fields.ForeignKeyField('{app_label}.Parent', related_name='children', "
            "on_delete=fields.CASCADE))], options={'table': 'fk_child'}),\n"
            "    ]\n"
        ),
        "0002_orphans": (
            "class Migration(migrations.Migration):\n"
            f"    atomic = {atomic!r}\n"
            f"    dependencies = [('{app_label}', '0001_initial')]\n"
            "    operations = [\n"
            "        ops.RunSQL(\n"
            "            [\n"
            "                'INSERT INTO fk_parent (id) VALUES (1)',\n"
            "                'INSERT INTO fk_child (id, parent_id) VALUES (1, 1)',\n"
            "                'INSERT INTO fk_child (id, parent_id) VALUES (2, 999)',\n"
            "                'DELETE FROM fk_parent WHERE id = 1',\n"
            "            ],\n"
            f"            atomic={operation_atomic!r},\n"
            "        ),\n"
            "    ]\n"
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("migration_atomic", "operation_atomic"),
    [(True, None), (False, True)],
    ids=["atomic_migration", "atomic_operation_in_non_atomic_migration"],
)
async def test_sqlite_migration_leaving_foreign_key_violations_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migration_atomic: bool, operation_atomic: bool | None
) -> None:
    """SQLite runs a migration with PRAGMA foreign_keys=OFF - dangling references created in that
    window must fail the migration (and roll it back) instead of being committed silently."""
    app_label = f"fkcheck{'atomic' if migration_atomic else 'peroperation'}app"
    module_path = _write_migration_sources(
        tmp_path,
        app_label,
        _foreign_key_violation_sources(app_label, atomic=migration_atomic, operation_atomic=operation_atomic),
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    async with HareContext() as ctx:
        ctx.connections._init_config(_sqlite_file_config(tmp_path / "fkcheck.sqlite3"))
        apps_config = {app_label: {"models": [], "default_connection": "default", "migrations": module_path}}
        connection = ctx.connections.get("default")
        executor = MigrationExecutor(connection, apps_config)

        with pytest.raises((IntegrityError, PartiallyAppliedMigrationError), match="foreign key violation"):
            await asyncio.wait_for(executor.migrate(), timeout=30)

        _, child_rows = await connection.execute("SELECT id FROM fk_child")
        assert child_rows == []
        applied = await MigrationRecorder(connection).applied_migrations()
        assert [key.name for key in applied] == ["0001_initial"]
        _, foreign_keys_rows = await connection.execute("PRAGMA foreign_keys")
        assert dict(foreign_keys_rows[0])["foreign_keys"] == 1

        await connection.close()


@pytest.mark.asyncio
async def test_sqlite_non_atomic_operation_leaving_foreign_key_violations_fails_the_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_path = _write_migration_sources(
        tmp_path,
        "fkchecknonatomicapp",
        _foreign_key_violation_sources("fkchecknonatomicapp", atomic=False, operation_atomic=None),
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    async with HareContext() as ctx:
        ctx.connections._init_config(_sqlite_file_config(tmp_path / "fkcheck_non_atomic.sqlite3"))
        apps_config = {
            "fkchecknonatomicapp": {"models": [], "default_connection": "default", "migrations": module_path}
        }
        connection = ctx.connections.get("default")
        executor = MigrationExecutor(connection, apps_config)

        with pytest.raises(PartiallyAppliedMigrationError, match="foreign key violation"):
            await asyncio.wait_for(executor.migrate(), timeout=30)

        applied = await MigrationRecorder(connection).applied_migrations()
        assert [key.name for key in applied] == ["0001_initial"]

        await connection.close()


def _write_mixed_target_migrations(tmp_path: Path) -> tuple[str, str]:
    other_module_path = _write_migrations(
        tmp_path, "mixedother", [("0001_initial", []), ("0002_second", [("mixedother", "0001_initial")])]
    )
    main_module_path = _write_migrations(
        tmp_path,
        "mixedmain",
        [("0001_initial", []), ("0002_needs_other", [("mixedmain", "0001_initial"), ("mixedother", "0002_second")])],
    )
    return other_module_path, main_module_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "targets",
    [
        [MigrationTarget("mixedother", "0001_initial"), MigrationTarget("mixedmain", "__latest__")],
        [MigrationTarget("mixedmain", "__latest__"), MigrationTarget("mixedother", "0001_initial")],
    ],
    ids=["rollback_then_forward", "forward_then_rollback"],
)
async def test_executor_plan_sees_earlier_targets_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, targets: list[MigrationTarget]
) -> None:
    """Every target is planned against the state earlier targets leave behind - a forward
    target must not skip a dependency that an earlier target rolls back, and a rollback must
    not skip a dependent that an earlier target applies."""
    other_module_path, main_module_path = _write_mixed_target_migrations(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    apps_config = {
        "mixedother": {"models": [], "default_connection": "default", "migrations": other_module_path},
        "mixedmain": {"models": [], "default_connection": "default", "migrations": main_module_path},
    }
    connection = FakeConnection(
        applied=[
            MigrationKey("mixedother", "0001_initial"),
            MigrationKey("mixedother", "0002_second"),
            MigrationKey("mixedmain", "0001_initial"),
        ]
    )
    executor = MigrationExecutor(cast(DatabaseClient, connection), apps_config)

    with pytest.raises(ValueError, match="Conflicting migration directions"):
        await executor.plan(targets)


@pytest.mark.asyncio
async def test_executor_plan_multiple_rollback_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other_module_path, main_module_path = _write_mixed_target_migrations(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    apps_config = {
        "mixedother": {"models": [], "default_connection": "default", "migrations": other_module_path},
        "mixedmain": {"models": [], "default_connection": "default", "migrations": main_module_path},
    }
    connection = FakeConnection(
        applied=[
            MigrationKey("mixedother", "0001_initial"),
            MigrationKey("mixedother", "0002_second"),
            MigrationKey("mixedmain", "0001_initial"),
            MigrationKey("mixedmain", "0002_needs_other"),
        ]
    )
    executor = MigrationExecutor(cast(DatabaseClient, connection), apps_config)

    steps = await executor.plan(
        [MigrationTarget("mixedmain", "0001_initial"), MigrationTarget("mixedother", "0001_initial")]
    )

    assert [(str(step.migration), step.backward) for step in steps] == [
        ("mixedmain.0002_needs_other", True),
        ("mixedother.0002_second", True),
    ]


@pytest.mark.asyncio
async def test_loader_picks_up_an_edited_migration_file_in_the_same_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration module already in sys.modules must be re-executed when its file changed -
    including a same-size edit within the same second, which a cached .pyc would not notice."""
    module_path = _write_migration_sources(
        tmp_path,
        "editedapp",
        {"0001_initial": "class Migration(migrations.Migration):\n    marker = 'aaa'\n"},
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    apps_config = {"editedapp": {"models": [], "default_connection": "default", "migrations": module_path}}
    loader = MigrationLoader(apps_config, MigrationRecorder(None))
    loader.load_disk()
    key = MigrationKey("editedapp", "0001_initial")
    assert type(loader.disk_migrations[key]).marker == "aaa"

    _write_migration_sources(
        tmp_path, "editedapp", {"0001_initial": "class Migration(migrations.Migration):\n    marker = 'bbb'\n"}
    )
    loader.load_disk()
    assert type(loader.disk_migrations[key]).marker == "bbb"

    # An unchanged file is not re-executed - module-level state survives a reload.
    module = importlib.import_module(f"{module_path}.0001_initial")
    module.loaded_state = "kept"
    loader.load_disk()
    assert module.loaded_state == "kept"
