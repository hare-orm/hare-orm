from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from hare.transactions.constants import DISTRIBUTED_DECISIONS_TABLE_NAME
from tests.utils.cli_commands import CliCommands
from tests.utils.project_sources import ProjectSources

pytestmark = pytest.mark.sqlite_only


def _write_settings(tmp_path: Path, content: str, module_name: str) -> str:
    ProjectSources(tmp_path).write_module(module_name, content)
    return module_name


def _write_settings_module(tmp_path: Path, db_path: Path) -> str:
    return _write_settings(
        tmp_path,
        f"""
HARE_ORM = {{
    "connections": {{"default": "sqlite+aiosqlite:///{db_path.as_posix()}"}},
    "apps": {{
        "app": {{"models": ["drift_app.models"], "default_connection": "default"}},
    }},
}}
""".lstrip(),
        f"drift_settings_{tmp_path.name}",
    )


async def _sync_database(module_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs makemigrations + migrate for the `app` app, leaving the database fully in sync with
    whatever `drift_app.models` currently declares."""
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    make_result = await CliCommands.run(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app"], project=tmp_path)
    assert make_result.exit_code == 0, make_result.output

    migrate_result = await CliCommands.run(["-c", f"{module_name}.HARE_ORM", "migrate"], project=tmp_path)
    assert migrate_result.exit_code == 0, migrate_result.output


@pytest.mark.asyncio
async def test_drift_clean_database_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = ProjectSources(tmp_path).write_package("drift_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "drift.db"
    module_name = _write_settings_module(tmp_path, db_path)
    await _sync_database(module_name, tmp_path, monkeypatch)

    result = await CliCommands.run(["-c", f"{module_name}.HARE_ORM", "drift"], project=tmp_path)
    assert result.exit_code == 0, result.output
    assert result.output == ""


@pytest.mark.asyncio
async def test_drift_reports_column_added_outside_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A column added via a raw ALTER TABLE (bypassing the migration history entirely) - drift
    must catch this, the live-check scenario this command exists for."""
    pkg = ProjectSources(tmp_path).write_package("drift_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "drift.db"
    module_name = _write_settings_module(tmp_path, db_path)
    await _sync_database(module_name, tmp_path, monkeypatch)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("ALTER TABLE widget ADD COLUMN extra_column TEXT")
        connection.commit()
    finally:
        connection.close()

    result = await CliCommands.run(["-c", f"{module_name}.HARE_ORM", "drift"], project=tmp_path)
    assert result.exit_code == 1, result.output
    assert "extra_column" in result.output
    assert "widget" in result.output


@pytest.mark.asyncio
async def test_drift_ignores_the_distributed_decisions_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hare_distributed_decisions (Transactions.distributed()'s own decision log, created by
    distributed()/distributed-recover) is hare's bookkeeping, not an untracked user table."""
    pkg = ProjectSources(tmp_path).write_package("drift_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
""".lstrip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "drift.db"
    module_name = _write_settings_module(tmp_path, db_path)
    await _sync_database(module_name, tmp_path, monkeypatch)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"CREATE TABLE {DISTRIBUTED_DECISIONS_TABLE_NAME} (xid TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    result = await CliCommands.run(["-c", f"{module_name}.HARE_ORM", "drift"], project=tmp_path)
    assert result.exit_code == 0, result.output
    assert result.output == ""


@pytest.mark.asyncio
async def test_drift_reports_missing_field_as_add_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A model field with no matching database column (e.g. added to the model but never
    migrated) shows up as a missing AddField, the same operation makemigrations would write."""
    pkg = ProjectSources(tmp_path).write_package("drift_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "drift.db"
    module_name = _write_settings_module(tmp_path, db_path)
    await _sync_database(module_name, tmp_path, monkeypatch)

    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    description = fields.CharField(max_length=200, null=True)
""".lstrip(),
        encoding="utf-8",
    )

    result = await CliCommands.run(["-c", f"{module_name}.HARE_ORM", "drift"], project=tmp_path)
    assert result.exit_code == 1, result.output
    assert "Add field description to Widget" in result.output


@pytest.mark.asyncio
async def test_drift_unknown_connection_raises_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = ProjectSources(tmp_path).write_package("drift_app")
    (pkg / "models.py").write_text(
        """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
""".lstrip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "drift.db"
    module_name = _write_settings_module(tmp_path, db_path)
    await _sync_database(module_name, tmp_path, monkeypatch)

    result = await CliCommands.run(
        ["-c", f"{module_name}.HARE_ORM", "drift", "--connection", "bogus"], project=tmp_path
    )
    assert result.exit_code == 2
    assert "Unknown connection alias" in result.output
