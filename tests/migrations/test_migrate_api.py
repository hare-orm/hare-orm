from pathlib import Path

import pytest

from hare import Hare
from hare.core.config import AppConfig, ConnectionConfig, HareConfig
from hare.core.connections.connections import Connections
from hare.migrations.api import migrate


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
    try:
        await migrate(config=config)
    finally:
        await Hare.close_connections()


def _write_create_model_migration(tmp_path: Path, app_label: str, model_name: str, table_name: str) -> str:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (package_dir / "models.py").write_text("", encoding="ascii")
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
    try:
        await migrate(config=config, target="appa.0001_initial")

        conn_a = Connections.get("conn_a")
        _, rows_a = await conn_a.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_a'")
        assert len(rows_a) == 1

        conn_b = Connections.get("conn_b")
        _, rows_b = await conn_b.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_b'")
        assert len(rows_b) == 0, "appb must not be migrated as a side effect of targeting appa"
    finally:
        await Hare.close_connections()


def _write_migration_with_dependencies(
    tmp_path: Path, app_label: str, model_name: str, table_name: str, dependencies: list[tuple[str, str]]
) -> str:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (package_dir / "models.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
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
    try:
        await migrate(config=config)

        conn_a = Connections.get("conn_a")
        _, rows_a = await conn_a.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_a'")
        _, rows_a_wrong = await conn_a.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_b'")
        conn_b = Connections.get("conn_b")
        _, rows_b = await conn_b.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_b'")
        _, rows_b_wrong = await conn_b.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_a'")
        assert len(rows_a) == 1, "appa must be migrated on its own connection (conn_a)"
        assert len(rows_a_wrong) == 0, "appb must never land on conn_a"
        assert len(rows_b) == 1, "appb must be migrated on its own connection (conn_b)"
        assert len(rows_b_wrong) == 0, "appa must never land on conn_b"
    finally:
        await Hare.close_connections()


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
    try:
        await migrate(config=config, target="appa")

        conn = Connections.get("default")
        _, rows_a = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_a'")
        _, rows_b = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='widget_b'")
        assert len(rows_a) == 1, "appa must still be migrated when explicitly targeted"
        assert len(rows_b) == 0, "appb must not be migrated as a side effect of targeting appa"
    finally:
        await Hare.close_connections()
