"""A HareContext over several fresh test databases, cleaned up like hare_test_context() cleans up one."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.core.hare_context import HareContext


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
        async with TemporaryDatabases(
            {"connections": dict.fromkeys(connection_aliases), "apps": apps},
            db_url,
            generate_schemas=False,
            routers=routers,
        ) as context:
            yield context
