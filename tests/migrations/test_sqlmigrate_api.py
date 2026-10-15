from pathlib import Path

import pytest

from hare import Hare
from hare.core.config import AppConfig, ConnectionConfig, HareConfig
from hare.migrations.api.sqlmigrate import sqlmigrate
from hare.migrations.exceptions import MigrationLoadError, UnknownMigrationError


def _sqlite_config(**app_kwargs) -> HareConfig:
    return HareConfig(
        connections={
            "default": ConnectionConfig(
                engine="sqlite+aiosqlite",
                credentials={"file_path": ":memory:"},
            )
        },
        apps={"models": AppConfig(models=["tests.testmodels"], default_connection="default", **app_kwargs)},
    )


@pytest.mark.asyncio
async def test_sqlmigrate_unknown_app_label():
    with pytest.raises(UnknownMigrationError, match="Unknown app label"):
        await sqlmigrate(config=_sqlite_config(), app_label="ghost", migration_name="0001_initial")


@pytest.mark.asyncio
async def test_sqlmigrate_missing_migration():
    with pytest.raises(MigrationLoadError):
        try:
            await sqlmigrate(config=_sqlite_config(), app_label="models", migration_name="0001_initial")
        finally:
            await Hare.close_connections()


def _write_migration_package(tmp_path: Path, app_label: str) -> str:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    return f"{app_label}.migrations"


def _write_empty_models_module(tmp_path: Path, app_label: str) -> str:
    """A models module with no Model subclasses - this test only needs the migration GRAPH to
    build correctly across apps, not any real model/relation resolution."""
    (tmp_path / f"{app_label}_models.py").write_text("", encoding="ascii")
    return f"{app_label}_models"


def _write_migration(
    tmp_path: Path,
    app_label: str,
    name: str,
    dependencies: list[tuple[str, str]] | None = None,
) -> str:
    module_path = _write_migration_package(tmp_path, app_label)
    migrations_dir = tmp_path / app_label / "migrations"
    content = "\n".join(
        [
            "from hare import migrations",
            "",
            "class Migration(migrations.Migration):",
            f"    dependencies = {dependencies or []!r}",
            "",
            "    operations = []",
            "",
        ]
    )
    (migrations_dir / f"{name}.py").write_text(content, encoding="ascii")
    return module_path


def _write_create_model_migration(
    tmp_path: Path, app_label: str, model_name: str, table_name: str, *, atomic: bool = True
) -> str:
    module_path = _write_migration_package(tmp_path, app_label)
    migrations_dir = tmp_path / app_label / "migrations"
    content = "\n".join(
        [
            "from hare import migrations",
            "from hare import fields",
            "from hare.migrations import operations as ops",
            "",
            "class Migration(migrations.Migration):",
            "    dependencies = []",
            f"    atomic = {atomic!r}",
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
    )
    (migrations_dir / "0001_initial.py").write_text(content, encoding="ascii")
    return module_path


@pytest.mark.asyncio
async def test_sqlmigrate_wraps_an_atomic_migration_in_begin_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """sqlite supports transactional DDL too (can_rollback_ddl=True) - a default
    (Migration.atomic=True) migration must be wrapped exactly like a real migrate() run would
    wrap it (MigrationExecutor.migrate()'s own atomic_migration handling), not left unwrapped
    just because the connection isn't Postgres."""
    monkeypatch.syspath_prepend(str(tmp_path))
    migrations_module = _write_create_model_migration(tmp_path, "atomic_app", "Widget", "widget")
    models_module = _write_empty_models_module(tmp_path, "atomic_app")
    config = HareConfig(
        connections={"default": ConnectionConfig(engine="sqlite+aiosqlite", credentials={"file_path": ":memory:"})},
        apps={
            "atomic_app": AppConfig(models=[models_module], default_connection="default", migrations=migrations_module)
        },
    )

    try:
        sql = await sqlmigrate(config=config, app_label="atomic_app", migration_name="0001_initial")
        assert sql[0] == "BEGIN;"
        assert sql[-1] == "COMMIT;"
    finally:
        await Hare.close_connections()


@pytest.mark.asyncio
async def test_sqlmigrate_does_not_wrap_a_non_atomic_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Migration.atomic = False (e.g. one doing a CREATE INDEX CONCURRENTLY build, which
    can't run inside a transaction at all on Postgres) must not get a misleading BEGIN;/
    COMMIT; wrapper - sqlmigrate() used to hardcode this decision from the connection's engine
    name alone, ignoring the migration's own atomic flag entirely."""
    monkeypatch.syspath_prepend(str(tmp_path))
    migrations_module = _write_create_model_migration(tmp_path, "nonatomic_app", "Widget", "widget", atomic=False)
    models_module = _write_empty_models_module(tmp_path, "nonatomic_app")
    config = HareConfig(
        connections={"default": ConnectionConfig(engine="sqlite+aiosqlite", credentials={"file_path": ":memory:"})},
        apps={
            "nonatomic_app": AppConfig(
                models=[models_module], default_connection="default", migrations=migrations_module
            )
        },
    )

    try:
        sql = await sqlmigrate(config=config, app_label="nonatomic_app", migration_name="0001_initial")
        assert "BEGIN;" not in sql
        assert "COMMIT;" not in sql
    finally:
        await Hare.close_connections()


@pytest.mark.asyncio
async def test_sqlmigrate_resolves_cross_app_migration_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """sqlmigrate() is always scoped to a single app_label, but a migration in that app can
    depend on a concrete migration in ANOTHER app (e.g. a FK to that app's model) - the loader
    must still load that other app's migrations to resolve the dependency, not crash with
    'references nonexistent parent' just because sqlmigrate only cares about one app's SQL."""
    monkeypatch.syspath_prepend(str(tmp_path))
    auth_module = _write_migration(tmp_path, "auth_app", "0001_initial")
    blog_module = _write_migration(tmp_path, "blog_app", "0001_initial", dependencies=[("auth_app", "0001_initial")])
    auth_models = _write_empty_models_module(tmp_path, "auth_app")
    blog_models = _write_empty_models_module(tmp_path, "blog_app")

    config = HareConfig(
        connections={"default": ConnectionConfig(engine="sqlite+aiosqlite", credentials={"file_path": ":memory:"})},
        apps={
            "auth_app": AppConfig(models=[auth_models], default_connection="default", migrations=auth_module),
            "blog_app": AppConfig(models=[blog_models], default_connection="default", migrations=blog_module),
        },
    )

    try:
        sql = await sqlmigrate(config=config, app_label="blog_app", migration_name="0001_initial")
        assert sql == []
    finally:
        await Hare.close_connections()
