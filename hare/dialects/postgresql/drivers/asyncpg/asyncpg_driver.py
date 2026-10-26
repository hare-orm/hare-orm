from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.connection.postgresql_driver import PostgresqlDriver
from hare.dialects.postgresql.constants import POSTGRES_CONNECTION_OPTIONS
from hare.dialects.postgresql.drivers.asyncpg.constants import ASYNCPG_CONNECTION_OPTIONS
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient


class AsyncpgDriver(PostgresqlDriver):
    """PostgreSQL through asyncpg."""

    name = "postgresql+asyncpg"
    url_schemes = ("postgresql+asyncpg",)
    connection_options = POSTGRES_CONNECTION_OPTIONS + ASYNCPG_CONNECTION_OPTIONS

    def get_client_class(self, credentials: dict[str, Any]) -> type[AsyncpgClient]:
        try:
            from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient
        except ImportError as error:
            raise ConfigurationError(
                "The postgresql+asyncpg engine needs asyncpg: pip install 'hare-orm[asyncpg]'"
            ) from error
        return AsyncpgClient

    def get_client_classes(self) -> tuple[type[DatabaseClient], ...]:
        try:
            from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient
            from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_transaction_client import (
                AsyncpgTransactionClient,
            )
        except ImportError:
            return ()
        return AsyncpgClient, AsyncpgTransactionClient

    def get_ssl_credentials(self, query_parameter: str, mode: str | bool) -> dict[str, Any]:
        return {"ssl": mode}


DialectRegistry.register_driver(AsyncpgDriver())
