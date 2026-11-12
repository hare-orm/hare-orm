from __future__ import annotations

import asyncio
import sqlite3

from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_connection_wrapper import AiosqliteConnectionWrapper


class AiosqliteTransactionConnectionWrapper(AiosqliteConnectionWrapper):
    """Marks the transaction aborted when a failed or interrupted statement made SQLite roll the
    whole transaction back on its own (an interrupted write, a trigger's RAISE(ROLLBACK)) - later
    statements would otherwise silently run in autocommit mode."""

    async def _after_statement(self, exception: BaseException | None) -> None:
        interrupted = isinstance(exception, (sqlite3.Error, asyncio.CancelledError))
        if interrupted and not self.connection.in_transaction:
            self.client._mark_transaction_aborted()  # type: ignore[attr-defined]
