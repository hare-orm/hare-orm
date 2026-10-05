from __future__ import annotations

import contextlib
import importlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hare.cli import hare_cli as cli_module

VALID_MIGRATION_SOURCE = """
from hare.migrations.migration import Migration


class Migration(Migration):
    pass
"""

BROKEN_MIGRATION_SOURCES = {
    "syntax_error": "this is not python(\n",
    "raises_on_import": "raise RuntimeError('boom')\n",
    "missing_import": "import nonexistent_module_for_error_tests\n",
}

GRAPH_COMMANDS = {
    "heads": ["heads"],
    "makemigrations": ["makemigrations"],
    "makemigrations_check": ["makemigrations", "--check"],
    "squashmigrations": ["squashmigrations", "broken", "0001_initial"],
    "sqlmigrate": ["sqlmigrate", "broken", "0001_initial"],
    "migrate": ["migrate"],
    "migrate_zero": ["migrate", "broken", "zero"],
}


def _purge_broken_modules() -> None:
    for module_name in list(sys.modules):
        if module_name.startswith("error_broken"):
            del sys.modules[module_name]


async def _run_cli(args: list[str]) -> SimpleNamespace:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = await cli_module.HareCLI.run_cli_async(args)
    return SimpleNamespace(exit_code=exit_code, output=stdout.getvalue() + stderr.getvalue())


def _write_broken_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migration_sources: dict[str, str]) -> str:
    package_path = tmp_path / "error_broken"
    package_path.mkdir()
    (package_path / "__init__.py").write_text("", encoding="utf-8")
    (package_path / "models.py").write_text("", encoding="utf-8")
    migrations_path = package_path / "migrations"
    migrations_path.mkdir()
    (migrations_path / "__init__.py").write_text("", encoding="utf-8")
    for migration_name, source in migration_sources.items():
        (migrations_path / f"{migration_name}.py").write_text(source, encoding="utf-8")
    settings_name = f"error_settings_{tmp_path.name}"
    (tmp_path / f"{settings_name}.py").write_text(
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "broken": {"models": ["error_broken.models"], "default_connection": "default"},
    },
}
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _purge_broken_modules()
    return f"{settings_name}.HARE_ORM"


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", sorted(GRAPH_COMMANDS))
@pytest.mark.parametrize("broken_type", sorted(BROKEN_MIGRATION_SOURCES))
async def test_graph_commands_report_unloadable_migration_file_as_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_name: str, broken_type: str
) -> None:
    config_argument = _write_broken_project(
        tmp_path,
        monkeypatch,
        {"0001_initial": VALID_MIGRATION_SOURCE, "0002_broken": BROKEN_MIGRATION_SOURCES[broken_type]},
    )
    try:
        result = await _run_cli(["-c", config_argument, *GRAPH_COMMANDS[command_name]])
    finally:
        _purge_broken_modules()

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "0002_broken" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", sorted(GRAPH_COMMANDS))
async def test_graph_commands_report_circular_dependency_as_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_name: str
) -> None:
    config_argument = _write_broken_project(
        tmp_path,
        monkeypatch,
        {
            "0004_a": VALID_MIGRATION_SOURCE.replace("pass", "dependencies = [('broken', '0005_b')]"),
            "0005_b": VALID_MIGRATION_SOURCE.replace("pass", "dependencies = [('broken', '0004_a')]"),
        },
    )
    graph_command = GRAPH_COMMANDS[command_name]
    if command_name == "sqlmigrate":
        graph_command = ["sqlmigrate", "broken", "0004_a"]
    try:
        result = await _run_cli(["-c", config_argument, *graph_command])
    finally:
        _purge_broken_modules()

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "ircular" in result.output


def _write_forked_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    depends_on_first = VALID_MIGRATION_SOURCE.replace("pass", "dependencies = [('broken', '0001_initial')]")
    return _write_broken_project(
        tmp_path,
        monkeypatch,
        {"0001_initial": VALID_MIGRATION_SOURCE, "0002_a": depends_on_first, "0002_b": depends_on_first},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_arguments", [[], ["--check"], ["--dry-run"], ["broken"]])
async def test_makemigrations_reports_conflicting_heads_instead_of_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_arguments: list[str]
) -> None:
    config_argument = _write_forked_project(tmp_path, monkeypatch)
    try:
        result = await _run_cli(["-c", config_argument, "makemigrations", *extra_arguments])
    finally:
        _purge_broken_modules()

    assert result.exit_code == 1
    assert "Conflicting migrations" in result.output
    assert "--merge" in result.output
    assert "No changes detected" not in result.output
    assert not list((tmp_path / "error_broken" / "migrations").glob("0003*"))


@pytest.mark.asyncio
async def test_makemigrations_merge_still_resolves_conflicting_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_argument = _write_forked_project(tmp_path, monkeypatch)
    try:
        result = await _run_cli(["-c", config_argument, "makemigrations", "--merge", "broken"])
    finally:
        _purge_broken_modules()

    assert result.exit_code == 0, result.output
    assert list((tmp_path / "error_broken" / "migrations").glob("0003_merge*"))
