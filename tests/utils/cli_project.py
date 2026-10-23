"""A throwaway project of models on disk, driven through the CLI the way a user runs it."""

from __future__ import annotations

import contextlib
import importlib
import io
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hare.cli import hare_cli as cli_module
from hare.core.hare_context import HareContext
from tests.utils.database_under_test import DatabaseUnderTest

MODELS_HEADER = (
    "from hare import fields\nfrom hare.ddl import RawSQLTerm\nfrom hare.ddl.constraints import UniqueConstraint\n"
    "from hare.models import Model\n"
)


class CliProject:
    """A throwaway project on disk (settings + one package per app) driven through the CLI."""

    def __init__(self, root: Path, database_url: str, app_labels: tuple[str, ...], settings_module: str) -> None:
        self.root = root
        self.database_url = database_url
        #: The connection the project's database was leased with - None for a SQLite file.
        self.leased_connection_config: dict[str, Any] | None = None
        #: The context query() runs on, opened by its first call - never current, so the CLI's own
        #: contexts don't see it.
        self.query_context: HareContext | None = None
        self.app_labels = app_labels
        self.settings_module = settings_module
        self.is_postgres = DatabaseUnderTest.get_dialect(database_url).name == "postgresql"
        apps_config = {
            app_label: {
                "models": [f"cli_rt_{app_label}.models"],
                "default_connection": "default",
                "migrations": f"cli_rt_{app_label}.migrations",
            }
            for app_label in app_labels
        }
        self.config = {"connections": {"default": database_url}, "apps": apps_config}
        (root / f"{settings_module}.py").write_text(f"HARE_ORM = {self.config!r}\n", encoding="utf-8")
        for app_label in app_labels:
            migrations_package = root / f"cli_rt_{app_label}" / "migrations"
            migrations_package.mkdir(parents=True)
            (root / f"cli_rt_{app_label}" / "__init__.py").write_text("", encoding="utf-8")
            (migrations_package / "__init__.py").write_text("", encoding="utf-8")
            self.write_models("", app_label)

    def write_models(self, source: str, app_label: str = "app") -> None:
        (self.root / f"cli_rt_{app_label}" / "models.py").write_text(
            MODELS_HEADER + textwrap.dedent(source), encoding="utf-8"
        )

    def write_migration(self, name: str, source: str, app_label: str = "app") -> None:
        (self.root / f"cli_rt_{app_label}" / "migrations" / f"{name}.py").write_text(
            textwrap.dedent(source), encoding="utf-8"
        )

    def migration_files(self, app_label: str = "app") -> list[str]:
        return sorted(path.stem for path in (self.root / f"cli_rt_{app_label}" / "migrations").glob("0*.py"))

    def read_latest_migration(self, app_label: str = "app") -> str:
        latest_name = self.migration_files(app_label)[-1]
        return (self.root / f"cli_rt_{app_label}" / "migrations" / f"{latest_name}.py").read_text(encoding="utf-8")

    async def cli(self, *args: str) -> SimpleNamespace:
        for module_name in [name for name in sys.modules if name.startswith("cli_rt_")]:
            del sys.modules[module_name]
        importlib.invalidate_caches()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = await cli_module.HareCLI.run_cli_async(["-c", f"{self.settings_module}.HARE_ORM", *args])
        return SimpleNamespace(exit_code=exit_code, output=stdout.getvalue() + stderr.getvalue())

    async def cli_ok(self, *args: str) -> str:
        result = await self.cli(*args)
        assert result.exit_code in (0, None), result.output
        return result.output

    async def query(self, sql: str) -> list[dict[str, Any]]:
        if self.query_context is None:
            query_context = HareContext()
            await query_context.init(config=get_probe_config(self.database_url))
            self.query_context = query_context
        return await self.query_context.get_connection("default").execute_dicts(sql)

    async def close_query_context(self) -> None:
        if self.query_context is not None:
            await self.query_context.connections.close_all(discard=False)
            self.query_context = None

    async def column_types(self, table: str) -> dict[str, str]:
        if self.is_postgres:
            rows = await self.query(
                "SELECT column_name, data_type FROM information_schema.columns "
                f"WHERE table_name = '{table}' AND table_schema = current_schema()"
            )
            return {row["column_name"]: row["data_type"] for row in rows}
        rows = await self.query(f"PRAGMA table_info('{table}')")
        return {row["name"]: row["type"].lower() for row in rows}

    async def assert_in_sync(self) -> None:
        check_result = await self.cli("makemigrations", "--check")
        assert check_result.exit_code in (0, None), check_result.output
        assert "No changes detected" in check_result.output
        drift_result = await self.cli("drift", *self.app_labels)
        assert drift_result.exit_code in (0, None), drift_result.output


def get_probe_config(connection: str | dict[str, Any]) -> dict[str, Any]:
    """A config opening a connection (a URL, or its expanded config) without importing any project app's
    models."""
    return {"connections": {"default": connection}, "apps": {"probe": {"models": ["cli_rt_probe"]}}}
