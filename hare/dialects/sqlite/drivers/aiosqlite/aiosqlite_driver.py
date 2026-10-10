from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.connection.sqlite_driver import SqliteDriver
from hare.dialects.sqlite.constants import SQLITE_AIOSQLITE_DRIVER_NAME, SQLITE_REGEXP_OPTION

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient


class AiosqliteDriver(SqliteDriver):
    """SQLite through aiosqlite and the stdlib ``sqlite3`` module - the driver of ``sqlite+aiosqlite://``."""

    name = SQLITE_AIOSQLITE_DRIVER_NAME
    url_schemes = (SQLITE_AIOSQLITE_DRIVER_NAME,)

    def get_client_class(self, credentials: dict[str, Any]) -> type[AiosqliteClient]:
        from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient
        from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client_with_regexp_support import (
            AiosqliteClientWithRegexpSupport,
        )

        install_regexp_functions = SQLITE_REGEXP_OPTION.parse(credentials.get(SQLITE_REGEXP_OPTION.name, False))
        return AiosqliteClientWithRegexpSupport if install_regexp_functions else AiosqliteClient

    def get_client_classes(self) -> tuple[type[DatabaseClient], ...]:
        from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient
        from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client_with_regexp_support import (
            AiosqliteClientWithRegexpSupport,
        )
        from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_transaction_client import (
            AiosqliteTransactionClient,
        )

        return AiosqliteClient, AiosqliteClientWithRegexpSupport, AiosqliteTransactionClient


DialectRegistry.register_driver(AiosqliteDriver())
