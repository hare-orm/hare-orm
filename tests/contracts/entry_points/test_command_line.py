"""Extra x entry point matrix, the command line column: in whatever environment the suite runs, a project in a
temporary folder gets its migration from ``makemigrations``, the SQL of it from ``sqlmigrate`` and its tables
from ``migrate``, and a second ``makemigrations`` finds nothing to do."""

import contextlib
import importlib
import io
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from hare.cli import hare_cli as cli_module
from hare.cli.plugins.cli_command_registry import CLICommandRegistry

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]

PROJECT_PREFIX = "entrypoint_project"
MODELS_SOURCE = """
from hare import fields
from hare.models import Model


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
"""


async def run_cli(arguments: list[str]) -> SimpleNamespace:
    """Runs the command line, giving its exit code and output."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = await cli_module.HareCLI.run_cli_async(arguments)
    return SimpleNamespace(exit_code=exit_code, output=stdout.getvalue() + stderr.getvalue())


async def test_a_project_is_migrated_by_the_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    package = tmp_path / f"{PROJECT_PREFIX}_app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (package / "models.py").write_text(textwrap.dedent(MODELS_SOURCE).lstrip(), encoding="utf-8", newline="\n")
    database = (tmp_path / "project.db").as_posix()
    (tmp_path / f"{PROJECT_PREFIX}_settings.py").write_text(
        f'HARE_ORM = {{"connections": {{"default": "sqlite+aiosqlite:///{database}"}}, '
        f'"apps": {{"app": {{"models": ["{PROJECT_PREFIX}_app.models"], "default_connection": "default"}}}}}}',
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(CLICommandRegistry, "get_entry_points", staticmethod(lambda: []))
    importlib.invalidate_caches()
    configuration = ["-c", f"{PROJECT_PREFIX}_settings.HARE_ORM"]
    try:
        made = await run_cli([*configuration, "makemigrations", "app"])
        assert made.exit_code == 0, made.output
        sql = await run_cli([*configuration, "sqlmigrate", "app", "0001"])
        assert sql.exit_code == 0, sql.output
        assert "CREATE TABLE" in sql.output
        migrated = await run_cli([*configuration, "migrate"])
        assert migrated.exit_code == 0, migrated.output
        again = await run_cli([*configuration, "makemigrations", "app"])
        assert again.exit_code == 0, again.output
        assert not list((package / "migrations").glob("0002_*.py"))
    finally:
        for module_name in [name for name in sys.modules if name.startswith(PROJECT_PREFIX)]:
            del sys.modules[module_name]
