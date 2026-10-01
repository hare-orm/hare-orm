"""A HareContext over several fresh test databases, cleaned up like hare_test_context() cleans up one."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from hare.contrib.test import ReusableTestDatabases
from hare.core.connection_handler import ConnectionHandler
from hare.core.context import HareContext
from hare.dialects.base.db_url import DbUrlConfigGenerator


class MultiDatabaseTestContext:
    """Opens a HareContext with one fresh test database per connection alias."""

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
        is_reusing_databases = ReusableTestDatabases.is_enabled()
        with ReusableTestDatabases.track_new_leases() as own_lease_number_by_database_name:
            connections_config = {
                alias: DbUrlConfigGenerator.expand(db_url, testing=True, reuse_databases=is_reusing_databases)
                for alias in connection_aliases
            }
        try:
            async with HareContext() as ctx:
                await ctx.init(
                    config={"connections": connections_config, "apps": apps}, routers=routers, _create_db=True
                )
                yield ctx
        finally:
            try:
                cleanup_connections = ConnectionHandler()
                cleanup_connections._init_config(dict(connections_config))
                for connection in cleanup_connections.all():
                    await connection.db_delete()
            finally:
                ReusableTestDatabases.release_leases(own_lease_number_by_database_name)
