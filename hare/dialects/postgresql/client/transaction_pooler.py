from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from hare.core.caching.cache import Cache
from hare.dialects.postgresql.client.constants import (
    POSTGRESQL_CURRENT_SEARCH_PATH_SQL,
    POSTGRESQL_REPLANNED_SQL_TEMPLATE,
)
from hare.exceptions import ConfigurationError
from hare.instrumentation.enums import PoolRole

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.client.postgresql_client import CoroutineFunction, PostgresqlClient

TPostgresqlClient = TypeVar("TPostgresqlClient", bound="PostgresqlClient")


class TransactionPooler:
    """What a client does when its connection goes through a pooler in transaction pooling
    (``transaction_pooling``; PgBouncer 1.21+): a server connection serves one transaction at a
    time and prepared statements are shared by their text, so a stale plan is prepared afresh, the
    search path is checked to reach the server, and work holding a session of its own goes to the
    server itself (``direct_host``/``direct_port``).
    """

    #: (connection name, statement) -> how many times the statement was prepared afresh past a
    #: transaction pooler after its plan went stale (``run_replanning_stale_statements()``).
    REPLANNED_STATEMENTS: ClassVar[Cache[int]] = Cache(
        Cache.max_size_from_env(), holds_sql=False, keyed_by_model=False
    )

    @staticmethod
    async def run_replanning_stale_statements(
        method: CoroutineFunction[Any], client: PostgresqlClient, sql: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Runs a statement past a pooler sharing prepared statements by their text: one whose plan a
        schema change made stale (its result altered) is run again under another text, which the
        pooler prepares afresh - and keeps that text for the statement's later runs.

        Args:
            method: The client method running the statement.
            client: The client - outside a transaction.
            sql: The statement.
            *args: The method's other arguments.
            **kwargs: The method's keyword arguments.

        Returns:
            What the method returned.
        """
        key = (client.connection_alias, sql)
        generation = TransactionPooler.REPLANNED_STATEMENTS.get(key)
        run_sql = (
            sql if generation is None else POSTGRESQL_REPLANNED_SQL_TEMPLATE.format(sql=sql, generation=generation)
        )
        try:
            return await method(client, run_sql, *args, **kwargs)
        except Exception as error:
            if not client.is_stale_plan_error(error):
                raise
        generation = (generation or 0) + 1
        TransactionPooler.REPLANNED_STATEMENTS[key] = generation
        return await method(
            client, POSTGRESQL_REPLANNED_SQL_TEMPLATE.format(sql=sql, generation=generation), *args, **kwargs
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
        await client.close()
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
        through - none for a connection going straight to the server.

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
