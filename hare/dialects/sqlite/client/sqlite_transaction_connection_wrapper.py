import asyncio
import sqlite3
from typing import TYPE_CHECKING, cast

import aiosqlite

from hare.dialects.sqlite.client.sqlite_connection_wrapper import SqliteConnectionWrapper

if TYPE_CHECKING:
    from hare.dialects.sqlite.client.sqlite_transaction_client import SqliteTransactionClient


class SqliteTransactionConnectionWrapper(SqliteConnectionWrapper):
    """Marks the transaction aborted when a failed or interrupted statement made SQLite roll the
    whole transaction back on its own (an interrupted write, a trigger's RAISE(ROLLBACK)) - later
    statements would otherwise silently run in autocommit mode."""

    async def __aenter__(self) -> aiosqlite.Connection:
        connection = await super().__aenter__()
        # The transaction's BEGIN and the SAVEPOINTs still pending go ahead of the statement.
        client: SqliteTransactionClient = self.client  # type: ignore[assignment]  # cast() is a call per statement
        transaction = client._outer_transaction or client
        if transaction._begin_pending or transaction._pending_savepoints:
            await self._send_pending_statements()
        return connection

    async def _after_statement(self, exc_val: BaseException | None) -> None:
        interrupted = isinstance(exc_val, sqlite3.Error | asyncio.CancelledError)
        if interrupted and not self.connection.in_transaction:
            cast("SqliteTransactionClient", self.client)._mark_transaction_aborted()
