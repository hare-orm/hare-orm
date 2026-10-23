from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.dialects.postgresql.client.constants import (
    POSTGRESQL_CURRENT_SEARCH_PATH_SQL,
)
from hare.dialects.postgresql.server_versions import POSTGRESQL_REPORTED_SEARCH_PATH_SERVER_VERSION
from hare.exceptions import ConfigurationError
from hare.instrumentation.enums import PoolRole

if TYPE_CHECKING:
    from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient

TPostgresqlClient = TypeVar("TPostgresqlClient", bound="PostgresqlClient")


class TransactionPooler:
    """What a client does when its connection goes through a pooler in transaction pooling
    (``transaction_pooling``; PgBouncer 1.22.1+): a server connection serves one transaction at a
    time, so the search path is checked to reach the server, and work holding a session of its own goes
    to the server itself (``direct_host``/``direct_port``).
    """

    @staticmethod
    async def raise_if_search_path_unreported(client: PostgresqlClient) -> None:
        """Refuses a search path behind a pooler on a server that doesn't tell the pooler its search
        path (PostgreSQL before 18) - the pooler can't restore it on the server connection it lends, and
        every schema's statements would run in the search path another client left.

        Args:
            client: The client.

        Raises:
            ConfigurationError: The server doesn't report its search path.
        """
        if client.server_version is None or client.server_version >= POSTGRESQL_REPORTED_SEARCH_PATH_SERVER_VERSION:
            return
        await client.close_refused_pool()
        raise ConfigurationError(
            f"The connection {client.connection_alias!r} sets a search path (the schema option or a schema per "
            "tenant) behind a transaction pooler - PostgreSQL before 18 doesn't tell the pooler its search path, "
            "so the pooler can't restore it on the server connection it lends: connect to PostgreSQL 18+, or "
            "straight to the server"
        )

    @staticmethod
    async def check_search_path(client: PostgresqlClient, expected_search_path: str) -> None:
        """Checks that the pooler passes the connection's search path on to the server - a pooler
        leaving it out would run every schema's statements in the server's default one.

        Args:
            client: The client.
            expected_search_path: The search path the connection sets.

        Raises:
            ConfigurationError: The statements run with another search path.
        """
        (row,) = await client.execute_dicts(POSTGRESQL_CURRENT_SEARCH_PATH_SQL)
        current_schemas = TransactionPooler.get_search_path_schemas(row["search_path"])
        if current_schemas == TransactionPooler.get_search_path_schemas(expected_search_path):
            return
        await client.close_refused_pool()
        raise ConfigurationError(
            f"The connection {client.connection_alias!r} goes through a transaction pooler that doesn't pass its "
            f"search path on: statements run with {row['search_path']!r}, not {expected_search_path!r} - "
            "PgBouncer needs `track_extra_parameters = search_path` (PgBouncer 1.20+)"
        )

    @staticmethod
    def get_search_path_schemas(search_path: str) -> tuple[str, ...]:
        """The schemas of a search path, as the server lists them - spaces and quotes dropped.

        Args:
            search_path: The search path.

        Returns:
            The schema names, in order.
        """
        return tuple(schema.strip().strip('"') for schema in search_path.split(",") if schema.strip())

    @staticmethod
    def get_direct_settings(client: PostgresqlClient) -> dict[str, Any]:
        """The settings of a client of the server itself, past the pooler the connection goes
        through, in the connection's schema - none for a connection going straight to the server.

        Args:
            client: The client.

        Returns:
            The settings replacing the connection's own.

        Raises:
            ConfigurationError: The connection goes through a pooler and names no direct way to the
                server.
        """
        if not client.transaction_pooling:
            return {}
        if client.direct_host is None:
            raise ConfigurationError(
                f"The connection {client.connection_alias!r} goes through a transaction pooler "
                "(transaction_pooling) - LISTEN, a session lock timeout and other work holding a session "
                "of its own needs the server's own address: set direct_host (and direct_port)"
            )
        return {
            "host": client.direct_host,
            "port": client.direct_port or client.port,
            "schema": client.schema,
            "transaction_pooling": False,
            "direct_host": None,
            "direct_port": None,
        }

    @staticmethod
    def get_direct_client(client: TPostgresqlClient) -> TPostgresqlClient:
        """The client of the server itself, past the pooler the connection goes through - the client
        itself for a connection going straight to the server. Kept until the client closes.

        Args:
            client: The client.

        Returns:
            The client of the server.

        Raises:
            ConfigurationError: The connection goes through a pooler and names no direct way to the
                server.
        """
        if not client.transaction_pooling:
            return client
        if client._direct_client is None:
            client._direct_client = cast(
                "TPostgresqlClient",
                client.create_independent_client({**TransactionPooler.get_direct_settings(client), "min_size": 0}),
            )
            client._direct_client.pool_role = PoolRole.DIRECT
        return client._direct_client
