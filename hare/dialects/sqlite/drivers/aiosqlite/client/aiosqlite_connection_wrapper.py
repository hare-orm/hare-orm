from __future__ import annotations

import asyncio
import sqlite3
from types import TracebackType

import aiosqlite

from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding


class AiosqliteConnectionWrapper(ConnectionWrapper[aiosqlite.Connection]):
    """Interrupts the statement a cancelled query (or one left by any other non-database
    exception) left running on aiosqlite's worker thread - it would otherwise keep the shared
    connection busy until it finished on its own, delaying every later query (and the ROLLBACK of
    its transaction) behind it."""

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        try:
            if exception is not None:
                if not isinstance(exception, sqlite3.Error):
                    await self._interrupt_abandoned_statement()
                await self._after_statement(exception)
        finally:
            await super().__aexit__(exception_type, exception, exception_traceback)

    async def _interrupt_abandoned_statement(self) -> None:
        """Interrupts whatever still runs on the worker thread, then waits - through any further
        cancellation - until the worker is idle, so the connection is free once released."""
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient

        # sqlite3_interrupt() is safe to call from any thread; a no-op when nothing runs.
        self.connection._conn.interrupt()
        try:
            await TransactionEnding.run_shielded_from_cancellation(
                AiosqliteClient._run_on_worker_thread(self.connection, AiosqliteConnectionWrapper._do_nothing),
                on_landed=lambda: None,
            )
        except ValueError:
            # aiosqlite's "no active connection" - closed meanwhile, nothing left running.
            pass

    @staticmethod
    def _do_nothing(raw_connection: sqlite3.Connection) -> None:
        """Runs on the worker thread only to mark the moment every earlier call there finished."""

    async def _after_statement(self, exception: BaseException | None) -> None:
        """Rolls back the transaction a cancelled ``execute_many()`` left open - outside a
        transaction every statement autocommits, so an open one can only be such a leftover.

        Args:
            exception: the exception the statement block raised, if any.
        """
        if isinstance(exception, asyncio.CancelledError) and self.connection.in_transaction:
            await TransactionEnding.run_shielded_from_cancellation(self.connection.rollback(), on_landed=lambda: None)
