from __future__ import annotations

from typing import Any, Self

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.client.constants import POSTGRESQL_SINGLE_CONNECTION_POOL_SETTINGS
from tests.utils.constants import SERVER_SESSION_CONNECTION_ALIAS
from tests.utils.database_under_test import DatabaseUnderTest


class ServerSession:
    """One session of the PostgreSQL server under test on a connection of its own - the test suite's
    bookkeeping (the run's ownership lock, sweeping leftover test databases, the server's version).
    Opened through hare's own driver of the URL under test, so a run needs no other driver: a run on
    ``rust_pg`` installs no asyncpg. Past a transaction pooler it is the server's own session
    (``direct_host``), a session-level lock lasting as long as it.

    Args:
        client: The client - a pool of one connection.
    """

    def __init__(self, client: DatabaseClient) -> None:
        self.client = client

    @classmethod
    async def open(cls, db_url: str, database: str, application_name: str | None = None) -> Self:
        """Opens a session of the server of a URL.

        Args:
            db_url: The URL of the database under test.
            database: The database the session connects to.
            application_name: The session's ``application_name``.

        Returns:
            The session.
        """
        settings = DbUrlConfigGenerator.expand(db_url, testing=False)
        credentials = dict(DatabaseUnderTest.get_direct_credentials(settings["credentials"]))
        credentials.update(POSTGRESQL_SINGLE_CONNECTION_POOL_SETTINGS, database=database)
        if application_name is not None:
            credentials["application_name"] = application_name
        client_class = DialectRegistry.get_driver(settings["engine"]).get_client_class(credentials)
        client = client_class(connection_alias=SERVER_SESSION_CONNECTION_ALIAS, **credentials)
        await client.create_connection(with_db=True)
        return cls(client)

    async def fetch(self, query: str, *values: Any) -> list[dict[str, Any]]:
        """The rows of a query.

        Args:
            query: The query - ``$1``-style parameters.
            values: The parameters.

        Returns:
            The rows.
        """
        return await self.client.execute_dicts(query, list(values))

    async def fetchval(self, query: str, *values: Any) -> Any:
        """The first value of a query's first row.

        Args:
            query: The query.
            values: The parameters.

        Returns:
            The value, None without a row.
        """
        rows = await self.fetch(query, *values)
        return next(iter(rows[0].values())) if rows else None

    async def execute(self, query: str, *values: Any) -> None:
        """Runs a statement - one without parameters as a script, outside any transaction block, which
        ``DROP DATABASE`` and ``ROLLBACK PREPARED`` need.

        Args:
            query: The statement.
            values: The parameters.
        """
        if values:
            await self.client.execute_dicts(query, list(values))
        else:
            await self.client.execute_script(query)

    async def close(self) -> None:
        """Closes the session."""
        await self.client.close()
