from pathlib import Path
from unittest.mock import Mock

import pytest

from hare.core.config import AppConfig, ConnectionConfig, HareConfig
from hare.migrations.api import MigrationSession, migrate
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.operations import CreateModel
from tests.utils.project_sources import ProjectSources

pytestmark = pytest.mark.sqlite_only


@pytest.mark.asyncio
async def test_migrate_accepts_dataclass_config() -> None:
    config = HareConfig(
        connections={
            "default": ConnectionConfig(
                engine="sqlite+aiosqlite",
                credentials={"file_path": ":memory:"},
            )
        },
        apps={"models": AppConfig(models=["tests.unmigrated.models"], default_connection="default")},
    )
    await migrate(config=config)


def _write_create_model_migration(tmp_path: Path, app_label: str, model_name: str, table_name: str) -> str:
    sources = ProjectSources(tmp_path)
    migrations_dir = sources.prepare_migrations(sources.write_package(app_label))
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
                f"            name={model_name!r},",
                "            fields=[",
                "                ('id', fields.IntField(generated=True, primary_key=True)),",
                "            ],",
                f"            options={{'table': {table_name!r}}},",
                "        ),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )
    return f"{app_label}.migrations"


@pytest.mark.asyncio
async def test_migrate_with_scoped_target_does_not_touch_unrelated_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scoped `target="appa.0001_initial"` used to silently full-migrate every OTHER app on
    every OTHER connection too: migrate()'s per-connection loop computed `executor_targets = [t
    for t in targets if t.app_label in subset]`, which is `[]` (falsy) for a connection that
    doesn't own the targeted app - indistinguishable from "no target was ever given at all", so
    `executor.migrate(None, ...)` ran and fully migrated that connection's own apps to their
    latest migration instead of doing nothing. Reproduces the exact CLI scenario (`hare migrate
    <app>` always resolves `app_labels=None` - every app - combined with a scoped `target`)."""
    appa_module = _write_create_model_migration(tmp_path, "appa", "WidgetA", "widget_a")
    appb_module = _write_create_model_migration(tmp_path, "appb", "WidgetB", "widget_b")
    monkeypatch.syspath_prepend(str(tmp_path))

    db_a = tmp_path / "a.sqlite3"
    db_b = tmp_path / "b.sqlite3"
    config = {
        "connections": {
            "conn_a": {"engine": "sqlite+aiosqlite", "credentials": {"file_path": str(db_a)}},
            "conn_b": {"engine": "sqlite+aiosqlite", "credentials": {"file_path": str(db_b)}},
        },
        "apps": {
            "appa": {"models": ["appa.models"], "default_connection": "conn_a", "migrations": appa_module},
            "appb": {"models": ["appb.models"], "default_connection": "conn_b", "migrations": appb_module},
        },
    }
    async with MigrationSession(config) as session:
        await session.migrate(target="appa.0001_initial")

        conn_a = session.context.get_connection("conn_a")
        _, rows_a = await conn_a.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_a'")
        assert len(rows_a) == 1

        conn_b = session.context.get_connection("conn_b")
        _, rows_b = await conn_b.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_b'")
        assert len(rows_b) == 0, "appb must not be migrated as a side effect of targeting appa"


def _write_migration_with_dependencies(
    tmp_path: Path, app_label: str, model_name: str, table_name: str, dependencies: list[tuple[str, str]]
) -> str:
    sources = ProjectSources(tmp_path)
    migrations_dir = sources.prepare_migrations(sources.write_package(app_label))
    (migrations_dir / "0001_initial.py").write_text(
        "\n".join(
            [
                "from hare import migrations",
                "from hare import fields",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                f"    dependencies = {dependencies!r}",
                "",
                "    operations = [",
                "        ops.CreateModel(",
                f"            name={model_name!r},",
                "            fields=[",
                "                ('id', fields.IntField(generated=True, primary_key=True)),",
                "            ],",
                f"            options={{'table': {table_name!r}}},",
                "        ),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )
    return f"{app_label}.migrations"


@pytest.mark.asyncio
async def test_migrate_resolves_a_cross_connection_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """appb (on conn_b) has an explicit `dependencies = [("appa", "0001_initial")]` entry naming
    a migration in appa, which lives on a DIFFERENT connection (conn_a) - migrate() used to group
    apps by connection FIRST and hand each connection's own MigrationExecutor only ITS OWN apps,
    so appa was never even loaded while building conn_b's graph: MigrationLoader.build_graph()'s
    consistency check raised "Migration appb.0001_initial references nonexistent parent
    appa.0001_initial" even though appa.0001_initial genuinely exists (just on another
    connection) and gets migrated in the very same migrate() call. Each connection's executor
    now builds its graph from every configured app (MigrationExecutor's own full_apps_config),
    while still only ever applying/reporting its own connection's apps - appa's migration must
    land on conn_a and appb's on conn_b, never swapped or duplicated onto the wrong connection."""
    appa_module = _write_create_model_migration(tmp_path, "appa", "WidgetA", "widget_a")
    appb_module = _write_migration_with_dependencies(
        tmp_path, "appb", "WidgetB", "widget_b", [("appa", "0001_initial")]
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    db_a = tmp_path / "a.sqlite3"
    db_b = tmp_path / "b.sqlite3"
    config = {
        "connections": {
            "conn_a": {"engine": "sqlite+aiosqlite", "credentials": {"file_path": str(db_a)}},
            "conn_b": {"engine": "sqlite+aiosqlite", "credentials": {"file_path": str(db_b)}},
        },
        "apps": {
            "appa": {"models": ["appa.models"], "default_connection": "conn_a", "migrations": appa_module},
            "appb": {"models": ["appb.models"], "default_connection": "conn_b", "migrations": appb_module},
        },
    }
    async with MigrationSession(config) as session:
        await session.migrate()

        conn_a = session.context.get_connection("conn_a")
        _, rows_a = await conn_a.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_a'")
        _, rows_a_wrong = await conn_a.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_b'")
        conn_b = session.context.get_connection("conn_b")
        _, rows_b = await conn_b.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_b'")
        _, rows_b_wrong = await conn_b.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_a'")
        assert len(rows_a) == 1, "appa must be migrated on its own connection (conn_a)"
        assert len(rows_a_wrong) == 0, "appb must never land on conn_a"
        assert len(rows_b) == 1, "appb must be migrated on its own connection (conn_b)"
        assert len(rows_b_wrong) == 0, "appa must never land on conn_b"


@pytest.mark.asyncio
async def test_migrate_scoped_target_still_finds_latest_when_another_app_depends_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both apps on the SAME connection - appb depends on appa. A SCOPED `target="appa"` (e.g.
    `hare migrate appa`) resolves to a single MigrationTarget(appa, "__latest__"), the only
    target `_migration_plan()` processes in this call - MigrationGraph.get_single_leaf("appa")
    used to look for a node with NO CHILD ANYWHERE IN THE GRAPH, so appa's own latest migration
    (genuinely a leaf within appa itself) stopped being found the moment appb's own migration
    declared a dependency on it, silently making `hare migrate appa` a no-op. Only reachable
    without a second, unrelated target in the same plan call happening to pull appa in via its
    own ancestor walk instead - the common no-args `hare migrate` (every app's target resolved
    together) never exposed this."""
    appa_module = _write_create_model_migration(tmp_path, "appa", "WidgetA", "widget_a")
    appb_module = _write_migration_with_dependencies(
        tmp_path, "appb", "WidgetB", "widget_b", [("appa", "0001_initial")]
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    db = tmp_path / "single.sqlite3"
    config = {
        "connections": {
            "default": {"engine": "sqlite+aiosqlite", "credentials": {"file_path": str(db)}},
        },
        "apps": {
            "appa": {"models": ["appa.models"], "default_connection": "default", "migrations": appa_module},
            "appb": {"models": ["appb.models"], "default_connection": "default", "migrations": appb_module},
        },
    }
    async with MigrationSession(config) as session:
        await session.migrate(target="appa")

        conn = session.context.get_connection("default")
        _, rows_a = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_a'")
        _, rows_b = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_b'")
        assert len(rows_a) == 1, "appa must still be migrated when explicitly targeted"
        assert len(rows_b) == 0, "appb must not be migrated as a side effect of targeting appa"


@pytest.mark.asyncio
async def test_migrate_creates_a_relation_without_a_constraint_to_another_connections_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appa_module = _write_create_model_migration(tmp_path, "relation_appa", "WidgetA", "widget_a")
    appb_module = _write_migration_with_dependencies(
        tmp_path, "relation_appb", "WidgetB", "widget_b", [("relation_appa", "0001_initial")]
    )
    migration_path = tmp_path / "relation_appb" / "migrations" / "0001_initial.py"
    migration_path.write_text(
        migration_path.read_text(encoding="ascii").replace(
            "                ('id', fields.IntField(generated=True, primary_key=True)),\n",
            "                ('id', fields.IntField(generated=True, primary_key=True)),\n"
            "                ('widget', fields.ForeignKeyField('relation_appa.WidgetA', db_constraint=False, "
            "null=True, on_delete=fields.SET_NULL)),\n",
        ),
        encoding="ascii",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    config = {
        "connections": {
            "conn_a": {"engine": "sqlite+aiosqlite", "credentials": {"file_path": str(tmp_path / "a.sqlite3")}},
            "conn_b": {"engine": "sqlite+aiosqlite", "credentials": {"file_path": str(tmp_path / "b.sqlite3")}},
        },
        "apps": {
            "relation_appa": {
                "models": ["relation_appa.models"],
                "default_connection": "conn_a",
                "migrations": appa_module,
            },
            "relation_appb": {
                "models": ["relation_appb.models"],
                "default_connection": "conn_b",
                "migrations": appb_module,
            },
        },
    }
    async with MigrationSession(config) as session:
        await session.migrate()
        _, columns = await session.context.get_connection("conn_b").execute(
            "SELECT name FROM pragma_table_info('widget_b')"
        )
        assert sorted(column[0] for column in columns) == ["id", "widget_id"]


@pytest.mark.asyncio
async def test_a_migration_waits_for_the_migration_of_another_connection_it_depends_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One plan over every connection: the migration of the first connection depends on a migration of
    the second - that one is applied first, and unapplied last."""
    provider_module = _write_create_model_migration(tmp_path, "provider", "Supply", "supply")
    dependent_module = _write_migration_with_dependencies(
        tmp_path, "dependent", "Order", "order_row", [("provider", "0001_initial")]
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    config = {
        "connections": {
            # The dependent app's connection comes first.
            "conn_first": {
                "engine": "sqlite+aiosqlite",
                "credentials": {"file_path": str(tmp_path / "first.sqlite3")},
            },
            "conn_second": {
                "engine": "sqlite+aiosqlite",
                "credentials": {"file_path": str(tmp_path / "second.sqlite3")},
            },
        },
        "apps": {
            "dependent": {
                "models": ["dependent.models"],
                "default_connection": "conn_first",
                "migrations": dependent_module,
            },
            "provider": {
                "models": ["provider.models"],
                "default_connection": "conn_second",
                "migrations": provider_module,
            },
        },
    }
    events: list[tuple[str, str]] = []

    async def record(event: str, app_label: str, name: str) -> None:
        events.append((event, app_label))

    async with MigrationSession(config) as session:
        await session.migrate(progress=record)
        assert events == [
            ("apply_start", "provider"),
            ("apply_done", "provider"),
            ("apply_start", "dependent"),
            ("apply_done", "dependent"),
        ]

        events.clear()
        await session.migrate(target="provider.zero", progress=record)
        assert events == [
            ("rollback_start", "dependent"),
            ("rollback_done", "dependent"),
            ("rollback_start", "provider"),
            ("rollback_done", "provider"),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_fake_migrate_does_not_replay_applied_state_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    app_label = "fake_state_app"
    migrations_module = _write_create_model_migration(tmp_path, app_label, "Book", "book")
    migrations_dir = tmp_path / app_label / "migrations"
    (migrations_dir / "0002_empty.py").write_text(
        "from hare import migrations\n"
        "class Migration(migrations.Migration):\n"
        f"    dependencies = [({app_label!r}, '0001_initial')]\n"
        "    operations = []\n",
        encoding="ascii",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    config = {
        "connections": {"default": {"engine": "sqlite+aiosqlite", "credentials": {"file_path": ":memory:"}}},
        "apps": {app_label: {"models": [], "default_connection": "default", "migrations": migrations_module}},
    }
    async with MigrationSession(config) as session:
        await session.migrate(target=f"{app_label}.0001_initial")
        state_callback = Mock(side_effect=AssertionError("fake migrations must not replay state callbacks"))
        monkeypatch.setattr(CreateModel, "state_forward", state_callback)
        await session.migrate(target=f"{app_label}.0002_empty", fake=True, dry_run=dry_run)
        state_callback.assert_not_called()
        executor = next(iter(session.create_executors().values()))
        expected = {MigrationKey(app_label, "0001_initial")}
        if not dry_run:
            expected.add(MigrationKey(app_label, "0002_empty"))
        assert set(await executor.recorder.applied_migrations()) == expected
