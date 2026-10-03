from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.postgresql.constants import POSTGRES_CONNECTION_OPTIONS
from hare.dialects.postgresql.driver import PostgresqlDriver
from hare.dialects.postgresql.drivers.asyncpg.constants import ASYNCPG_CONNECTION_OPTIONS
from hare.dialects.registry import DialectRegistry

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient


class AsyncpgDriver(PostgresqlDriver):
    """PostgreSQL through asyncpg."""

    name = "postgresql+asyncpg"
    url_schemes = ("postgresql+asyncpg",)
    connection_options = POSTGRES_CONNECTION_OPTIONS + ASYNCPG_CONNECTION_OPTIONS

    def get_client_class(self, credentials: dict[str, Any]) -> type[AsyncpgClient]:
        from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient

        return AsyncpgClient

    def get_client_classes(self) -> tuple[type[DatabaseClient], ...]:
        from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient
        from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_transaction_client import AsyncpgTransactionClient

        return AsyncpgClient, AsyncpgTransactionClient

    def get_ssl_credentials(self, query_param: str, mode: str | bool) -> dict[str, Any]:
        return {"ssl": mode}


DialectRegistry.register_driver(AsyncpgDriver())
