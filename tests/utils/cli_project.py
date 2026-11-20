"""A throwaway project of models on disk, driven through the CLI the way a user runs it."""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hare.core.hare_context import HareContext
from hare.fields.field import Field
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.migration import Migration
from tests.utils.cli_commands import CliCommands
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.project_imports import ProjectImports
from tests.utils.project_sources import ProjectSources

MODELS_HEADER = (
    "from hare import fields\nfrom hare.ddl import RawSQLTerm\nfrom hare.ddl.constraints import UniqueConstraint\n"
    "from hare.models import Model\n"
)


class CliProject:
    """A throwaway project on disk (settings + one package per app) driven through the CLI."""

    def __init__(self, root: Path, database_url: str, app_labels: tuple[str, ...], settings_module: str) -> None:
        self.root = root
        self.imports = ProjectImports(root)
        self.sources = ProjectSources(root)
        self.database_url = database_url
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
        self.sources.write_module(settings_module, f"HARE_ORM = {self.config!r}\n")
        for app_label in app_labels:
            self.sources.write_package(f"cli_rt_{app_label}")
            self.sources.write_module(f"cli_rt_{app_label}/migrations/__init__", "")
            self.write_models("", app_label)

    def write_models(self, source: str, app_label: str = "app") -> None:
        self.sources.write_module(f"cli_rt_{app_label}/models", MODELS_HEADER + textwrap.dedent(source))

    def write_migration(self, name: str, source: str, app_label: str = "app") -> None:
        self.sources.write_migration(self.root / f"cli_rt_{app_label}", name, source)

    def migration_files(self, app_label: str = "app") -> list[str]:
        return sorted(path.stem for path in (self.root / f"cli_rt_{app_label}" / "migrations").glob("0*.py"))

    def read_latest_migration(self, app_label: str = "app") -> str:
        latest_name = self.migration_files(app_label)[-1]
        return (self.root / f"cli_rt_{app_label}" / "migrations" / f"{latest_name}.py").read_text(encoding="utf-8")

    def get_latest_migration(self, app_label: str = "app") -> Migration:
        """Import the generated migration and inspect its actual operations and dependencies."""
        return self.get_migration(self.migration_files(app_label)[-1], app_label)

    def get_migration(self, name: str, app_label: str = "app") -> Migration:
        """Load a selected migration with the declaration scope used by the migration loader."""
        with self.imports.activate(), Field.replaying_migration_scope():
            module = importlib.import_module(f"cli_rt_{app_label}.migrations.{name}")
            return module.Migration(name, app_label)

    async def cli(self, *args: str) -> SimpleNamespace:
        with self.imports.activate():
            return await CliCommands.run(["-c", f"{self.settings_module}.HARE_ORM", *args])

    async def cli_ok(self, *args: str) -> str:
        result = await self.cli(*args)
        assert result.exit_code in (0, None), result.output
        return result.output

    async def query(self, sql: str) -> list[dict[str, Any]]:
        context = await self.get_query_context()
        return await context.get_connection("default").execute_dicts(sql)

    async def get_query_context(self) -> HareContext:
        if self.query_context is None:
            query_context = HareContext()
            try:
                await query_context.init(config=get_probe_config(self.database_url))
            except BaseException:
                await query_context.close_connections()
                raise
            self.query_context = query_context
        return self.query_context

    async def close_query_context(self) -> None:
        if self.query_context is not None:
            try:
                await self.query_context.close_connections()
            finally:
                self.query_context = None

    async def column_types(self, table: str) -> dict[str, str]:
        context = await self.get_query_context()
        info = await DatabaseCatalog.inspect_table(context.get_connection(), table)
        return {column.name: column.db_type.lower() for column in info.columns}

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
