from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from hare.core.resource_completion import ResourceCompletion
from hare.dialects.sqlite.constants import SQLITE_PRAGMA_ENABLED_VALUES, SQLITE_PRAGMA_RESTORE_FAILED

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.sqlite.client.sqlite_client import SqliteClient


class SqlitePragmaScope:
    """Restore an internal connection setting even when enabling it is interrupted.

    Names and values come from dialect code, never user SQL. The setting is read before
    changing it unless the caller supplies its configured restoration value. Restoration
    finishes before cancellation propagates, and preserves an earlier operation failure.
    """

    @staticmethod
    async def set_value(connection: DatabaseClient, name: str, value: str | int, previous: str | int) -> None:
        """Set a known internal PRAGMA and publish changes to physical connection state.

        SQLite ignores foreign_keys setters inside a transaction. Its observed previous
        value therefore remains authoritative there, including during restoration.
        """
        await connection.execute(f"PRAGMA {name} = {value}")
        if name == "foreign_keys":
            client: SqliteClient = connection  # type: ignore[assignment]
            actual = previous if connection.is_transaction_client else value
            client.get_physical_client().foreign_keys_enabled = str(actual).lower() in SQLITE_PRAGMA_ENABLED_VALUES

    @staticmethod
    @asynccontextmanager
    async def run(
        connection: DatabaseClient, name: str, value: str | int, *, previous: str | int | None = None
    ) -> AsyncGenerator[None]:
        if previous is None:
            result = await connection.execute(f"PRAGMA {name}")
            previous = result.rows[0][0]
        try:
            await SqlitePragmaScope.set_value(connection, name, value, previous)
            yield
        except BaseException as error:
            with ResourceCompletion.preserve_error(error, SQLITE_PRAGMA_RESTORE_FAILED):
                await ResourceCompletion.wait(SqlitePragmaScope.set_value(connection, name, previous, previous))
            raise
        else:
            await ResourceCompletion.wait(SqlitePragmaScope.set_value(connection, name, previous, previous))
