from __future__ import annotations

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from hare.cli.hare_cli import HareCLI
from tests.utils.project_imports import ProjectImports


class CliCommands:
    """Run the real asynchronous CLI entrypoint and retain both output streams."""

    @staticmethod
    async def run(arguments: list[str], *, project: Path | None = None) -> SimpleNamespace:
        stdout, stderr = StringIO(), StringIO()
        imports = ProjectImports(project).isolate_modules() if project is not None else nullcontext()
        with imports, redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = await HareCLI.run_cli_async(arguments)
        return SimpleNamespace(
            exit_code=exit_code,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
            output=stdout.getvalue() + stderr.getvalue(),
        )
