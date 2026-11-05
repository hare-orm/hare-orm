from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from tests.utils.cli_commands import CliCommands
from tests.utils.project_sources import ProjectSources

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


def _write_broken_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migration_sources: dict[str, str]) -> str:
    sources = ProjectSources(tmp_path)
    package_path = sources.write_package("error_broken")
    sources.write_module("error_broken/migrations/__init__", "")
    for migration_name, source in migration_sources.items():
        sources.write_migration(package_path, migration_name, source)
    settings_name = f"error_settings_{tmp_path.name}"
    sources.write_module(
        settings_name,
        """
HARE_ORM = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {
        "broken": {"models": ["error_broken.models"], "default_connection": "default"},
    },
}
""".lstrip(),
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return f"{settings_name}.HARE_ORM"


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_name", "broken_type"),
    [
        # The loader turns every broken file into MigrationLoadError, each command reports that: every
        # broken file on one command, one broken file on each other command.
        *[("heads", broken_type) for broken_type in sorted(BROKEN_MIGRATION_SOURCES)],
        *[(command_name, "syntax_error") for command_name in sorted(GRAPH_COMMANDS) if command_name != "heads"],
    ],
)
async def test_graph_commands_report_unloadable_migration_file_as_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_name: str, broken_type: str
) -> None:
    config_argument = _write_broken_project(
        tmp_path,
        monkeypatch,
        {"0001_initial": VALID_MIGRATION_SOURCE, "0002_broken": BROKEN_MIGRATION_SOURCES[broken_type]},
    )
    result = await CliCommands.run(["-c", config_argument, *GRAPH_COMMANDS[command_name]], project=tmp_path)

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
    result = await CliCommands.run(["-c", config_argument, *graph_command], project=tmp_path)

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


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("extra_arguments", [[], ["--check"], ["--dry-run"], ["broken"]])
async def test_makemigrations_reports_conflicting_heads_instead_of_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_arguments: list[str]
) -> None:
    config_argument = _write_forked_project(tmp_path, monkeypatch)
    result = await CliCommands.run(["-c", config_argument, "makemigrations", *extra_arguments], project=tmp_path)

    assert result.exit_code == 1
    assert "Conflicting migrations" in result.output
    assert "--merge" in result.output
    assert "No changes detected" not in result.output
    assert not list((tmp_path / "error_broken" / "migrations").glob("0003*"))


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_makemigrations_merge_still_resolves_conflicting_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_argument = _write_forked_project(tmp_path, monkeypatch)
    result = await CliCommands.run(["-c", config_argument, "makemigrations", "--merge", "broken"], project=tmp_path)

    assert result.exit_code == 0, result.output
    assert list((tmp_path / "error_broken" / "migrations").glob("0003_merge*"))
