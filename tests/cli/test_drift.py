from __future__ import annotations

import contextlib
import importlib
import io
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hare.cli import hare_cli as cli_module
from hare.transactions.constants import DISTRIBUTED_DECISIONS_TABLE_NAME


def _write_package(tmp_path: Path, name: str) -> Path:
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text("", encoding="utf-8")
    return pkg


def _write_settings(tmp_path: Path, content: str, module_name: str) -> str:
    (tmp_path / f"{module_name}.py").write_text(content, encoding="utf-8")
    return module_name


def _purge_drift_app_modules() -> None:
    """drift_app is reused (same dotted name, different tmp_path) across tests in this file -
    a stale cached models module from an earlier test would otherwise win over the just-written
    file, the same reload gotcha test_cli.py's own _purge_cli_app_modules works around."""
    for mod in list(sys.modules):
        if mod.startswith("drift_app"):
            del sys.modules[mod]


async def _run_cli(args: list[str]) -> SimpleNamespace:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = await cli_module.HareCLI.run_cli_async(args)
    return SimpleNamespace(exit_code=exit_code, output=stdout.getvalue() + stderr.getvalue())


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
    _purge_drift_app_modules()

    make_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "makemigrations", "app"])
    assert make_result.exit_code == 0, make_result.output
    _purge_drift_app_modules()

    migrate_result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "migrate"])
    assert migrate_result.exit_code == 0, migrate_result.output
    _purge_drift_app_modules()


@pytest.mark.asyncio
async def test_drift_clean_database_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _write_package(tmp_path, "drift_app")
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

    try:
        await _sync_database(module_name, tmp_path, monkeypatch)

        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "drift"])
        assert result.exit_code == 0, result.output
        assert result.output == ""
    finally:
        _purge_drift_app_modules()


@pytest.mark.asyncio
async def test_drift_reports_column_added_outside_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A column added via a raw ALTER TABLE (bypassing the migration history entirely) - drift
    must catch this, the live-check scenario this command exists for."""
    pkg = _write_package(tmp_path, "drift_app")
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

    try:
        await _sync_database(module_name, tmp_path, monkeypatch)

        connection = sqlite3.connect(db_path)
        try:
            connection.execute("ALTER TABLE widget ADD COLUMN extra_column TEXT")
            connection.commit()
        finally:
            connection.close()

        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "drift"])
        assert result.exit_code == 1, result.output
        assert "extra_column" in result.output
        assert "widget" in result.output
    finally:
        _purge_drift_app_modules()


@pytest.mark.asyncio
async def test_drift_ignores_the_distributed_decisions_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hare_distributed_decisions (Transactions.distributed()'s own decision log, created by
    distributed()/distributed-recover) is hare's bookkeeping, not an untracked user table."""
    pkg = _write_package(tmp_path, "drift_app")
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

    try:
        await _sync_database(module_name, tmp_path, monkeypatch)

        connection = sqlite3.connect(db_path)
        try:
            connection.execute(f"CREATE TABLE {DISTRIBUTED_DECISIONS_TABLE_NAME} (xid TEXT PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()

        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "drift"])
        assert result.exit_code == 0, result.output
        assert result.output == ""
    finally:
        _purge_drift_app_modules()


@pytest.mark.asyncio
async def test_drift_reports_missing_field_as_add_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A model field with no matching database column (e.g. added to the model but never
    migrated) shows up as a missing AddField, the same operation makemigrations would write."""
    pkg = _write_package(tmp_path, "drift_app")
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

    try:
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
        _purge_drift_app_modules()

        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "drift"])
        assert result.exit_code == 1, result.output
        assert "Add field description to Widget" in result.output
    finally:
        _purge_drift_app_modules()


@pytest.mark.asyncio
async def test_drift_unknown_connection_raises_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _write_package(tmp_path, "drift_app")
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

    try:
        await _sync_database(module_name, tmp_path, monkeypatch)

        result = await _run_cli(["-c", f"{module_name}.HARE_ORM", "drift", "--connection", "bogus"])
        assert result.exit_code == 2
        assert "Unknown connection alias" in result.output
    finally:
        _purge_drift_app_modules()
