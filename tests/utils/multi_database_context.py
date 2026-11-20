"""A HareContext over several fresh test databases, cleaned up like hare_test_context() cleans up one."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.contrib.test.databases.test_database_config import TestDatabaseConfig
from hare.core.hare_context import HareContext
from tests.utils.database_under_test import DatabaseUnderTest

if TYPE_CHECKING:
    from pathlib import Path


class MultiDatabaseTestContext:
    """Opens a HareContext with one fresh test database per connection alias."""

    @staticmethod
    @asynccontextmanager
    async def open_fixed_sqlite_routes(
        tmp_path: Path, aliases: Sequence[str], *, app_label: str, module_name: str, router: type
    ) -> AsyncGenerator[HareContext]:
        """Open each named SQLite file with the same schema and the first alias as default."""
        connections = {
            alias: TestDatabaseConfig.from_url(
                DatabaseUnderTest.get_sqlite_file_url(tmp_path / f"{alias}.sqlite")
            ).config
            for alias in aliases
        }
        async with HareContext() as context:
            await context.init(
                config={
                    "connections": connections,
                    "apps": {app_label: {"models": [module_name], "default_connection": aliases[0]}},
                },
                routers=[router],
                _create_db=True,
            )
            await context.generate_schemas()
            first_connection = context.connections.get(aliases[0])
            for alias in aliases[1:]:
                await context.connections.get(alias).execute_script(first_connection.get_schema_sql(safe=True))
            yield context

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        db_url: str,
        connection_aliases: Iterable[str],
        apps: dict[str, dict[str, Any]],
        *,
        routers: list[str | type] | None = None,
    ) -> AsyncGenerator[HareContext]:
        """Creates a database per alias from a test URL and removes them all on exit.

        Postgres databases come from ReusableTestDatabases when HARE_TEST_REUSE_DATABASES is on,
        and are reset instead of dropped. The databases are removed through connections of their
        own, so a body that closes or replaces the context's connections still leaves nothing behind.

        Args:
            db_url: The test database URL, usually with a "{}" placeholder.
            connection_aliases: One connection alias per database.
            apps: The "apps" section of the config.
            routers: Database routers of the context.

        Returns:
            The initialized context; schemas are not generated.
        """
        async with TemporaryDatabases(
            {"connections": dict.fromkeys(connection_aliases), "apps": apps},
            db_url,
            generate_schemas=False,
            routers=routers,
        ) as context:
            yield context
