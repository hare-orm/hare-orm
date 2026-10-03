"""The `hare` CLI's handling of broken configurations: a clean message and a non-zero exit code,
never a traceback."""

from __future__ import annotations

import contextlib
import importlib
import io
import sqlite3
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from hare.cli import hare_cli as cli_module
from hare.cli.plugins import CLICommandRegistry


async def _run_cli(args: list[str]) -> SimpleNamespace:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = await cli_module.HareCLI.run_cli_async(args)
    return SimpleNamespace(exit_code=exit_code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def _write_module(tmp_path: Path, name: str, source: str) -> None:
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")


def _use_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module_prefix: str) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(CLICommandRegistry, "get_entry_points", staticmethod(lambda: []))
    importlib.invalidate_caches()
    for module_name in list(sys.modules):
        is_migrations_module = module_name == "migrations" or module_name.startswith("migrations.")
        if module_name.startswith(module_prefix) or is_migrations_module:
            del sys.modules[module_name]


MODELS_SOURCE = """
from hare import fields
from hare.models import Model


class {name}(Model):
    id = fields.IntField(primary_key=True)
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection", "expected_message"),
    [
        ('{"engine": "hare.dialects.nosuch", "credentials": {}}', 'Unknown database engine "hare.dialects.nosuch"'),
        ('{"engine": "sqlite", "credentials": {}}', "needs a non-empty file_path"),
        (
            '{"engine": "postgresql+asyncpg", "credentials": {"port": "abc"}}',
            "port must be a whole number",
        ),
        (
            '"sqlite://:memory:?jurnal_mode=WAL"',
            "Unknown connection parameter(s) ['jurnal_mode'] for the sqlite driver",
        ),
    ],
)
async def test_history_with_a_broken_connection_config_exits_cleanly(
    tmp_path, monkeypatch, connection, expected_message
):
    _write_module(tmp_path, "cfgerr_models", MODELS_SOURCE.format(name="CfgErrWidget"))
    _write_module(
        tmp_path,
        "cfgerr_settings",
        f'HARE_ORM = {{"connections": {{"default": {connection}}}, '
        '"apps": {"models": {"models": ["cfgerr_models"]}}}',
    )
    _use_project(tmp_path, monkeypatch, "cfgerr_")

    result = await _run_cli(["-c", "cfgerr_settings.HARE_ORM", "history"])

    assert result.exit_code == 1
    assert expected_message in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_name", "exit_message"),
    [("ConfigurationError", "ConfigurationError: plugin config problem"), ("DBConnectionError", "DBConnectionError")],
)
async def test_plugin_command_raising_a_hare_error_exits_cleanly(tmp_path, monkeypatch, exception_name, exit_message):
    _write_module(tmp_path, "cfgerr_plugin_models", MODELS_SOURCE.format(name="CfgErrPluginWidget"))
    _write_module(
        tmp_path,
        "cfgerr_plugin_commands",
        f"""
        from hare.cli.plugins import CLICommand
        from hare.exceptions import {exception_name}


        class FailingCommand(CLICommand):
            name = "fail"

            async def run(self, ctx, args):
                raise {exception_name}("plugin config problem")
        """,
    )
    _write_module(
        tmp_path,
        "cfgerr_plugin_settings",
        'HARE_ORM = {"connections": {"default": "sqlite://:memory:"}, '
        '"apps": {"models": {"models": ["cfgerr_plugin_models"]}}, '
        '"cli": {"commands": ["cfgerr_plugin_commands:FailingCommand"]}}',
    )
    _use_project(tmp_path, monkeypatch, "cfgerr_plugin")

    result = await _run_cli(["-c", "cfgerr_plugin_settings.HARE_ORM", "fail"])

    assert result.exit_code == 1
    assert exit_message in result.stderr
    assert "plugin config problem" in result.stderr


def _write_two_single_file_apps(tmp_path: Path, explicit_migrations: bool) -> None:
    _write_module(tmp_path, "cfgerr_app_a", MODELS_SOURCE.format(name="CfgErrLog"))
    _write_module(tmp_path, "cfgerr_app_b", MODELS_SOURCE.format(name="CfgErrThing"))
    migrations_a = ', "migrations": "cfgerr_migrations_a"' if explicit_migrations else ""
    migrations_b = ', "migrations": "cfgerr_migrations_b"' if explicit_migrations else ""
    _write_module(
        tmp_path,
        "cfgerr_two_settings",
        f'HARE_ORM = {{"connections": {{"default": "sqlite://{(tmp_path / "db.sqlite3").as_posix()}"}}, '
        f'"apps": {{"a": {{"models": ["cfgerr_app_a"]{migrations_a}}}, '
        f'"b": {{"models": ["cfgerr_app_b"]{migrations_b}}}}}}}',
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["init", "makemigrations", "migrate", "heads"])
async def test_two_single_file_apps_sharing_an_inferred_migrations_package_are_refused(tmp_path, monkeypatch, command):
    """Both apps inferred the same top-level "migrations" package - makemigrations wrote both apps'
    0001_initial into one file (the second overwriting the first) and migrate then failed."""
    _write_two_single_file_apps(tmp_path, explicit_migrations=False)
    _use_project(tmp_path, monkeypatch, "cfgerr_")

    result = await _run_cli(["-c", "cfgerr_two_settings.HARE_ORM", command])

    assert result.exit_code == 1
    assert 'Apps "a" and "b" would both keep their migrations in "migrations"' in result.stderr
    assert not (tmp_path / "migrations").exists()


@pytest.mark.asyncio
async def test_two_single_file_apps_with_explicit_migrations_packages_migrate(tmp_path, monkeypatch):
    _write_two_single_file_apps(tmp_path, explicit_migrations=True)
    _use_project(tmp_path, monkeypatch, "cfgerr_")

    make_result = await _run_cli(["-c", "cfgerr_two_settings.HARE_ORM", "makemigrations"])
    assert make_result.exit_code == 0, make_result.stdout + make_result.stderr
    migrate_result = await _run_cli(["-c", "cfgerr_two_settings.HARE_ORM", "migrate"])
    assert migrate_result.exit_code == 0, migrate_result.stdout + migrate_result.stderr

    assert (tmp_path / "cfgerr_migrations_a" / "0001_initial.py").exists()
    assert (tmp_path / "cfgerr_migrations_b" / "0001_initial.py").exists()
    with contextlib.closing(sqlite3.connect(tmp_path / "db.sqlite3")) as connection:
        table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"cfgerrlog", "cfgerrthing"} <= table_names


@pytest.mark.asyncio
async def test_single_file_app_keeps_its_inferred_migrations_package(tmp_path, monkeypatch):
    _write_module(tmp_path, "cfgerr_single_app", MODELS_SOURCE.format(name="CfgErrSingle"))
    _write_module(
        tmp_path,
        "cfgerr_single_settings",
        'HARE_ORM = {"connections": {"default": "sqlite://:memory:"}, '
        '"apps": {"models": {"models": ["cfgerr_single_app"]}}}',
    )
    _use_project(tmp_path, monkeypatch, "cfgerr_single")

    result = await _run_cli(["-c", "cfgerr_single_settings.HARE_ORM", "init"])

    assert result.exit_code == 0, result.stderr
    assert (tmp_path / "migrations" / "__init__.py").exists()
