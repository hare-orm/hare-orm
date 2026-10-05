"""migrate's lock timeout: a migration whose statement waits longer for a lock another session holds
fails instead of queueing behind it - from ``migrate(lock_timeout=...)``, ``hare migrate
--lock-timeout`` or the config's ``migrations.lock_timeout``."""

import importlib
import sqlite3
import time
from pathlib import Path
from typing import Any, cast

import pytest

from hare import Hare
from hare.core.config.hare_config import HareConfig
from hare.core.config.migrations_config import MigrationsConfig
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import ConfigurationError, QueryError
from hare.migrations.api import migrate
from hare.migrations.execution.migration_runner import MigrationRunner
from hare.transactions.transaction_options import TransactionOptions

INVALID_TIMEOUTS = ["5", True, -1, 0, 10**12, [1]]
CONFIG_BASE: dict[str, Any] = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
}


@pytest.mark.parametrize("lock_timeout", INVALID_TIMEOUTS)
def test_an_invalid_lock_timeout_is_refused_everywhere(lock_timeout):
    with pytest.raises(ConfigurationError, match="lock_timeout"):
        MigrationsConfig(lock_timeout=lock_timeout)
    with pytest.raises(ConfigurationError, match="lock_timeout"):
        HareConfig.from_dict({**CONFIG_BASE, "migrations": {"lock_timeout": lock_timeout}})
    with pytest.raises(ConfigurationError, match="lock_timeout"):
        MigrationRunner(cast("DatabaseClient", object()), lock_timeout=lock_timeout)
    with pytest.raises(QueryError, match="lock_timeout"):
        TransactionOptions(lock_timeout=lock_timeout)


def test_the_migrations_section_takes_only_lock_timeout():
    config = HareConfig.from_dict({**CONFIG_BASE, "migrations": {"lock_timeout": 2.5}})
    assert config.migrations == MigrationsConfig(lock_timeout=2.5)
    assert config.to_dict()["migrations"] == {"lock_timeout": 2.5}
    with pytest.raises(ConfigurationError, match="did you mean"):
        HareConfig.from_dict({**CONFIG_BASE, "migrations": {"lock_timout": 1}})


WIDGET_MIGRATION = """
from hare import fields, migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    operations = [
        ops.CreateModel(
            name="Widget", fields=[("id", fields.IntField(primary_key=True))], options={"table": "widget"}
        ),
    ]
"""
ADD_SIZE_MIGRATION = """
from hare import fields, migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    atomic = {atomic}
    dependencies = [("app", "0001_initial")]
    operations = [ops.AddField(model_name="Widget", name="size", field=fields.IntField(default=0))]
"""


def write_project(tmp_path: Path, *, atomic: bool, lock_timeout: float | None) -> dict[str, Any]:
    package_name = f"lock_timeout_app_{tmp_path.name}"
    migrations_path = tmp_path / package_name / "migrations"
    migrations_path.mkdir(parents=True)
    (tmp_path / package_name / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / package_name / "models.py").write_text("", encoding="utf-8")
    (migrations_path / "__init__.py").write_text("", encoding="utf-8")
    (migrations_path / "0001_initial.py").write_text(WIDGET_MIGRATION.lstrip(), encoding="utf-8")
    (migrations_path / "0002_add_size.py").write_text(
        ADD_SIZE_MIGRATION.format(atomic=atomic).lstrip(), encoding="utf-8"
    )
    importlib.invalidate_caches()
    config: dict[str, Any] = {
        "connections": {
            "default": {"engine": "sqlite+aiosqlite", "credentials": {"file_path": str(tmp_path / "db.sqlite3")}}
        },
        "apps": {
            "app": {
                "models": [f"{package_name}.models"],
                "default_connection": "default",
                "migrations": f"{package_name}.migrations",
            }
        },
    }
    if lock_timeout is not None:
        config["migrations"] = {"lock_timeout": lock_timeout}
    return config


def read_journal(tmp_path: Path) -> set[str]:
    with sqlite3.connect(tmp_path / "db.sqlite3") as connection:
        return {row[0] for row in connection.execute("SELECT name FROM hare_migrations")}


@pytest.mark.asyncio
@pytest.mark.parametrize("atomic", [True, False])
@pytest.mark.parametrize("from_config", [True, False])
async def test_sqlite_migration_fails_fast_behind_another_connections_lock(tmp_path, monkeypatch, atomic, from_config):
    monkeypatch.syspath_prepend(str(tmp_path))
    config = write_project(tmp_path, atomic=atomic, lock_timeout=0.2 if from_config else None)
    try:
        await migrate(config=config, target="app.0001_initial", lock_timeout=None if from_config else 0.2)
    finally:
        await Hare.close_connections()
    blocker = sqlite3.connect(tmp_path / "db.sqlite3", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started_at = time.perf_counter()
        with pytest.raises(Exception, match="locked"):
            try:
                await migrate(config=config, lock_timeout=None if from_config else 0.2)
            finally:
                await Hare.close_connections()
        # Well before SQLite's own five-second busy timeout.
        assert time.perf_counter() - started_at < 3
        assert read_journal(tmp_path) == {"0001_initial"}
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    try:
        await migrate(config=config, lock_timeout=None if from_config else 0.2)
    finally:
        await Hare.close_connections()
    assert read_journal(tmp_path) == {"0001_initial", "0002_add_size"}
