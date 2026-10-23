from __future__ import annotations

import asyncio
import sqlite3
from types import TracebackType

import aiosqlite

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_transaction_connection_wrapper import (
    AiosqliteTransactionConnectionWrapper,
)


class AiosqliteStatementTimeoutConnectionWrapper(AiosqliteTransactionConnectionWrapper):
    """Interrupts a query still running once its transaction's statement timeout has passed -
    SQLite has no server-side statement timeout of its own."""

    __slots__ = ("_statement_timeout", "_interrupt_handle", "_interrupted")

    def __init__(self, lock: asyncio.Lock, client: DatabaseClient, statement_timeout: float) -> None:
        super().__init__(lock, client)
        self._statement_timeout = statement_timeout
        self._interrupt_handle: asyncio.TimerHandle | None = None
        self._interrupted = False

    async def __aenter__(self) -> aiosqlite.Connection:
        connection = await super().__aenter__()
        self._interrupt_handle = asyncio.get_running_loop().call_later(self._statement_timeout, self._interrupt)
        return connection

    def _interrupt(self) -> None:
        self._interrupted = True
        # sqlite3_interrupt() is safe to call from any thread - the query itself runs on
        # aiosqlite's worker thread.
        self.connection._conn.interrupt()

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        if self._interrupt_handle is not None:
            self._interrupt_handle.cancel()
        try:
            if self._interrupted and isinstance(exception, sqlite3.OperationalError):
                message = f"canceling statement due to statement timeout ({self._statement_timeout}s)"
                if not self.connection.in_transaction:
                    message += "; SQLite rolled back the whole transaction"
                raise sqlite3.OperationalError(message) from exception
        finally:
            await super().__aexit__(exception_type, exception, exception_traceback)
