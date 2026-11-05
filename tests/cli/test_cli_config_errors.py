"""The `hare` CLI's handling of broken configurations: a clean message and a non-zero exit code,
never a traceback."""

from __future__ import annotations

import contextlib
import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

from hare.cli.plugins.cli_command_registry import CLICommandRegistry
from tests.utils.cli_commands import CliCommands
from tests.utils.project_sources import ProjectSources


def _use_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(CLICommandRegistry, "get_entry_points", staticmethod(lambda: []))
    importlib.invalidate_caches()


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
        ('{"engine": "hare.dialects.nosuch", "credentials": {}}', "Unknown database engine 'hare.dialects.nosuch'"),
        ('{"engine": "sqlite+aiosqlite", "credentials": {}}', "needs a non-empty file_path"),
        pytest.param(
            '{"engine": "postgresql", "credentials": {"port": "abc"}}',
            "port must be a whole number",
            marks=pytest.mark.requires("native"),
        ),
        (
            '"sqlite+aiosqlite://:memory:?jurnal_mode=WAL"',
            "Unknown connection parameter(s) ['jurnal_mode'] for the sqlite driver",
        ),
    ],
)
async def test_history_with_a_broken_connection_config_exits_cleanly(
    tmp_path, monkeypatch, connection, expected_message
):
    ProjectSources(
        tmp_path,
    ).write_module("cfgerr_models", MODELS_SOURCE.format(name="CfgErrWidget"))
    ProjectSources(
        tmp_path,
    ).write_module(
        "cfgerr_settings",
        f'HARE_ORM = {{"connections": {{"default": {connection}}}, '
        '"apps": {"models": {"models": ["cfgerr_models"]}}}',
    )
    _use_project(
        tmp_path,
        monkeypatch,
    )

    result = await CliCommands.run(["-c", "cfgerr_settings.HARE_ORM", "history"], project=tmp_path)

    assert result.exit_code == 1
    assert expected_message in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_models_module_failing_as_it_is_imported_exits_cleanly(tmp_path, monkeypatch):
    """A models module raising an error of its own as it was imported - not a missing name or broken
    syntax - came out of the CLI as a traceback."""
    ProjectSources(
        tmp_path,
    ).write_module("cfgmodraise_models", 'raise RuntimeError("boom at import")')
    ProjectSources(
        tmp_path,
    ).write_module(
        "cfgmodraise_settings",
        'HARE_ORM = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, '
        '"apps": {"models": {"models": ["cfgmodraise_models"]}}}',
    )
    _use_project(
        tmp_path,
        monkeypatch,
    )

    result = await CliCommands.run(["-c", "cfgmodraise_settings.HARE_ORM", "history"], project=tmp_path)

    assert result.exit_code == 1
    assert "'cfgmodraise_models' failed to import: RuntimeError: boom at import" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.asyncio
async def test_a_configuration_module_failing_as_it_is_imported_exits_cleanly(tmp_path, monkeypatch):
    ProjectSources(
        tmp_path,
    ).write_module("cfgraise_settings", 'raise RuntimeError("no secrets in this environment")')
    _use_project(
        tmp_path,
        monkeypatch,
    )

    result = await CliCommands.run(["-c", "cfgraise_settings.HARE_ORM", "heads"], project=tmp_path)

    assert result.exit_code == 1
    assert "'cfgraise_settings' failed to import: RuntimeError: no secrets in this environment" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.requires("pyright")
@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_stubs_into_a_path_that_is_a_file_exit_cleanly(tmp_path, monkeypatch):
    ProjectSources(
        tmp_path,
    ).write_module("cfgstub_models", MODELS_SOURCE.format(name="CfgStubWidget"))
    ProjectSources(
        tmp_path,
    ).write_module(
        "cfgstub_settings",
        'HARE_ORM = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, '
        '"apps": {"models": {"models": ["cfgstub_models"]}}}',
    )
    (tmp_path / "typings").write_text("not a folder", encoding="utf-8")
    _use_project(
        tmp_path,
        monkeypatch,
    )

    result = await CliCommands.run(
        ["-c", "cfgstub_settings.HARE_ORM", "stubs", "--output", "typings"], project=tmp_path
    )

    assert result.exit_code == 1
    assert "Can't write the stubs into 'typings'" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_name", "exit_message"),
    [("ConfigurationError", "ConfigurationError: plugin config problem"), ("DBConnectionError", "DBConnectionError")],
)
async def test_plugin_command_raising_a_hare_error_exits_cleanly(tmp_path, monkeypatch, exception_name, exit_message):
    ProjectSources(
        tmp_path,
    ).write_module("cfgerr_plugin_models", MODELS_SOURCE.format(name="CfgErrPluginWidget"))
    ProjectSources(
        tmp_path,
    ).write_module(
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
    ProjectSources(
        tmp_path,
    ).write_module(
        "cfgerr_plugin_settings",
        'HARE_ORM = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, '
        '"apps": {"models": {"models": ["cfgerr_plugin_models"]}}, '
        '"cli": {"commands": ["cfgerr_plugin_commands:FailingCommand"]}}',
    )
    _use_project(
        tmp_path,
        monkeypatch,
    )

    result = await CliCommands.run(["-c", "cfgerr_plugin_settings.HARE_ORM", "fail"], project=tmp_path)

    assert result.exit_code == 1
    assert exit_message in result.stderr
    assert "plugin config problem" in result.stderr


def _write_two_single_file_apps(tmp_path: Path, explicit_migrations: bool) -> None:
    ProjectSources(
        tmp_path,
    ).write_module("cfgerr_app_a", MODELS_SOURCE.format(name="CfgErrLog"))
    ProjectSources(
        tmp_path,
    ).write_module("cfgerr_app_b", MODELS_SOURCE.format(name="CfgErrThing"))
    migrations_a = ', "migrations": "cfgerr_migrations_a"' if explicit_migrations else ""
    migrations_b = ', "migrations": "cfgerr_migrations_b"' if explicit_migrations else ""
    ProjectSources(
        tmp_path,
    ).write_module(
        "cfgerr_two_settings",
        f'HARE_ORM = {{"connections": {{"default": "sqlite+aiosqlite://{(tmp_path / "db.sqlite3").as_posix()}"}}, '
        f'"apps": {{"a": {{"models": ["cfgerr_app_a"]{migrations_a}}}, '
        f'"b": {{"models": ["cfgerr_app_b"]{migrations_b}}}}}}}',
    )


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["init", "makemigrations", "migrate", "heads"])
async def test_two_single_file_apps_sharing_an_inferred_migrations_package_are_refused(tmp_path, monkeypatch, command):
    """Both apps inferred the same top-level "migrations" package - makemigrations wrote both apps'
    0001_initial into one file (the second overwriting the first) and migrate then failed."""
    _write_two_single_file_apps(tmp_path, explicit_migrations=False)
    _use_project(
        tmp_path,
        monkeypatch,
    )

    result = await CliCommands.run(["-c", "cfgerr_two_settings.HARE_ORM", command], project=tmp_path)

    assert result.exit_code == 1
    assert "Apps 'a' and 'b' would both keep their migrations in 'migrations'" in result.stderr
    assert not (tmp_path / "migrations").exists()


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_two_single_file_apps_with_explicit_migrations_packages_migrate(tmp_path, monkeypatch):
    _write_two_single_file_apps(tmp_path, explicit_migrations=True)
    _use_project(
        tmp_path,
        monkeypatch,
    )

    make_result = await CliCommands.run(["-c", "cfgerr_two_settings.HARE_ORM", "makemigrations"], project=tmp_path)
    assert make_result.exit_code == 0, make_result.stdout + make_result.stderr
    migrate_result = await CliCommands.run(["-c", "cfgerr_two_settings.HARE_ORM", "migrate"], project=tmp_path)
    assert migrate_result.exit_code == 0, migrate_result.stdout + migrate_result.stderr

    assert (tmp_path / "cfgerr_migrations_a" / "0001_initial.py").exists()
    assert (tmp_path / "cfgerr_migrations_b" / "0001_initial.py").exists()
    with contextlib.closing(sqlite3.connect(tmp_path / "db.sqlite3")) as connection:
        table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"cfgerrlog", "cfgerrthing"} <= table_names


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_single_file_app_keeps_its_inferred_migrations_package(tmp_path, monkeypatch):
    ProjectSources(
        tmp_path,
    ).write_module("cfgerr_single_app", MODELS_SOURCE.format(name="CfgErrSingle"))
    ProjectSources(
        tmp_path,
    ).write_module(
        "cfgerr_single_settings",
        'HARE_ORM = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, '
        '"apps": {"models": {"models": ["cfgerr_single_app"]}}}',
    )
    _use_project(
        tmp_path,
        monkeypatch,
    )

    result = await CliCommands.run(["-c", "cfgerr_single_settings.HARE_ORM", "init"], project=tmp_path)

    assert result.exit_code == 0, result.stderr
    assert (tmp_path / "migrations" / "__init__.py").exists()


@pytest.mark.requires("pyright")
@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="'//' is the root of no server on Windows only")
async def test_stubs_into_a_path_no_stub_name_goes_into_exit_cleanly(tmp_path, monkeypatch):
    """ "//" made pathlib raise ValueError as a stub's name was put into it - a traceback."""
    ProjectSources(
        tmp_path,
    ).write_module("cfgstubroot_models", MODELS_SOURCE.format(name="CfgStubRootWidget"))
    ProjectSources(
        tmp_path,
    ).write_module(
        "cfgstubroot_settings",
        'HARE_ORM = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, '
        '"apps": {"models": {"models": ["cfgstubroot_models"]}}}',
    )
    _use_project(
        tmp_path,
        monkeypatch,
    )

    arguments = ["-c", "cfgstubroot_settings.HARE_ORM", "stubs", "--output", "//"]
    result = await CliCommands.run(arguments, project=tmp_path)
    assert result.exit_code == 1
    assert "Can't write the stubs into '//'" in result.stderr
    assert "Traceback" not in result.stderr

    checked = await CliCommands.run([*arguments, "--check"], project=tmp_path)
    assert checked.exit_code == 1
    assert "Outdated stub:" in checked.stdout or "Can't write the stubs into '//'" in checked.stderr
    assert "Traceback" not in checked.stderr
