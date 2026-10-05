from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncGenerator
from typing import Any

import aiosqlite

from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.transaction_lifecycle.pending_statements import PendingStatements
from hare.dialects.sqlite.client.sqlite_client import SqliteClient
from hare.dialects.sqlite.client.sqlite_transaction_client import SqliteTransactionClient
from hare.dialects.sqlite.constants import SQLITE_NULL_BYTE, SQLITE_TRANSACTION_ABORTED_MESSAGE
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_statement_timeout_connection_wrapper import (
    AiosqliteStatementTimeoutConnectionWrapper,
)
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_transaction_connection_wrapper import (
    AiosqliteTransactionConnectionWrapper,
)
from hare.dialects.sqlite.drivers.constants import SQLITE_FIRST_NUMBERED_PLACEHOLDER, SQLITE_STREAM_BATCH_SIZE
from hare.exceptions import TransactionManagementError


class AiosqliteTransactionClient(SqliteTransactionClient, AiosqliteClient):
    """A transaction of an aiosqlite connection: its statements, cursors and transaction control,
    each a hop to the connection's worker thread."""

    _connection: aiosqlite.Connection

    def acquire_connection(self) -> ConnectionWrapper[aiosqlite.Connection]:
        statement_timeout = self._transaction_options.statement_timeout
        if statement_timeout is None:
            return AiosqliteTransactionConnectionWrapper(self._lock, self)
        return AiosqliteStatementTimeoutConnectionWrapper(self._lock, self, statement_timeout)

    async def _execute_on_aborted_transaction(self, query: str) -> None:
        async with self.acquire_connection() as connection:
            await connection.execute(query)

    async def _driver_stream_batches(
        self, query: str, values: list[Any] | None = None, chunk_size: int = 0
    ) -> AsyncGenerator[list[sqlite3.Row]]:
        """Reads the rows of ``query`` off a cursor of this transaction's connection, ``chunk_size``
        rows per hop to aiosqlite's worker thread - the whole result is never held at once. Each
        fetch holds the transaction's connection lock, never across a ``yield``: the same task may run
        another query between fetches. The cursor is closed when the stream ends, is closed or is
        cancelled.

        Args:
            query: The tagged SQL.
            values: The bound values.
            chunk_size: How many rows a batch holds - ``SQLITE_STREAM_BATCH_SIZE`` when 0.

        Raises:
            TransactionManagementError: The transaction is aborted.
        """
        self._check_statement_allowed()
        if self._is_transaction_aborted():
            raise TransactionManagementError(SQLITE_TRANSACTION_ABORTED_MESSAGE)
        if SQLITE_NULL_BYTE in query:
            query = self._escape_null_bytes(query)
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("%s: %s", query, values)
        if values and self.binds_parameters_by_number and SQLITE_FIRST_NUMBERED_PLACEHOLDER in query:
            values = self.get_values_by_number(values)  # type: ignore[assignment]
        batch_size = chunk_size or SQLITE_STREAM_BATCH_SIZE
        cursor = await self._open_stream_cursor(query, values or [])
        try:
            while True:
                self._check_statement_allowed()
                rows = await self._fetch_stream_rows(cursor, batch_size)
                if not rows:
                    return
                yield rows
        finally:
            await asyncio.shield(self._run_on_worker_thread(self._connection, self._close_cursor, cursor))

    @SqliteClient.translate_exceptions
    async def _open_stream_cursor(self, query: str, values: list[Any]) -> sqlite3.Cursor:
        """Runs ``query`` on a cursor of the transaction's connection, its rows left on the cursor."""
        async with self.acquire_connection() as connection:
            return await self._run_on_worker_thread(connection, self._execute_on_cursor, query, values)

    @SqliteClient.translate_exceptions
    async def _fetch_stream_rows(self, cursor: sqlite3.Cursor, batch_size: int) -> list[sqlite3.Row]:
        """The next ``batch_size`` rows of a streamed cursor - fewer, or none, at its end."""
        async with self.acquire_connection() as connection:
            return await self._run_on_worker_thread(connection, self._fetch_many, cursor, batch_size)

    @staticmethod
    def _execute_on_cursor(raw_connection: sqlite3.Connection, query: str, values: list[Any]) -> sqlite3.Cursor:
        """Runs a statement on aiosqlite's worker thread and gives its cursor - of rows read by
        name too."""
        cursor = raw_connection.cursor()
        # typeshed types a cursor's row factory as a callable of (cursor, row) - sqlite3.Row is one.
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        return cursor.execute(query, values)

    @staticmethod
    def _fetch_many(_raw_connection: sqlite3.Connection, cursor: sqlite3.Cursor, batch_size: int) -> list[sqlite3.Row]:
        """Fetches rows of a cursor on aiosqlite's worker thread."""
        return cursor.fetchmany(batch_size)

    @staticmethod
    def _close_cursor(_raw_connection: sqlite3.Connection, cursor: sqlite3.Cursor) -> None:
        """Closes a cursor on aiosqlite's worker thread."""
        cursor.close()

    @SqliteClient.translate_exceptions
    async def execute_many(self, query: str, values: list[list[Any]]) -> None:
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug("%s: %s", query, values)
            if self.binds_parameters_by_number and SQLITE_FIRST_NUMBERED_PLACEHOLDER in query:
                values = [self.get_values_by_number(row) for row in values]  # type: ignore[misc]
            # Already within transaction, so ideal for performance
            await connection.executemany(query, values)

    @SqliteClient.translate_exceptions
    async def execute_script(self, query: str) -> None:
        """Runs the script's statements one by one inside this transaction - sqlite3's own
        executescript() would commit the open transaction before running anything."""
        async with self.acquire_connection() as connection:
            self.log.debug(query)
            await self._run_on_worker_thread(connection, self._execute_statements, self.split_script(query))

    async def _driver_send_begin(self) -> None:
        # The commit of a leftover transaction and the BEGIN go to aiosqlite's worker thread in one
        # hop - it runs a queued call to completion whether or not the awaiting task lives.
        await self._run_on_worker_thread(self._connection, self._commit_leftover_and_begin)

    @staticmethod
    def _commit_leftover_and_begin(raw_connection: sqlite3.Connection) -> None:
        """Commits a transaction still open on the connection, then starts a new one - runs on
        aiosqlite's worker thread.

        Args:
            raw_connection: The underlying sqlite3 connection.
        """
        raw_connection.commit()
        raw_connection.execute("BEGIN")

    async def _driver_commit(self) -> None:
        await self._connection.commit()

    async def _driver_rollback(self) -> None:
        await self._connection.rollback()

    async def _driver_savepoint(self, name: str) -> None:
        # A SAVEPOINT outside a transaction would start one its RELEASE commits.
        await PendingStatements.send_pending_begin(self)
        await self._connection.execute(f"SAVEPOINT {name}")

    async def _driver_release_savepoint(self, name: str) -> None:
        await self._connection.execute(f"RELEASE {name}")

    async def _driver_rollback_to_savepoint(self, name: str) -> None:
        # SQLite may already have rolled the whole transaction back, the savepoint with it - the
        # transaction is then marked aborted.
        if self._is_transaction_aborted():
            return
        try:
            await self._connection.execute(f"ROLLBACK TO {name}")
        except sqlite3.OperationalError:
            if self._connection.in_transaction:
                raise
            self._mark_transaction_aborted()

    async def _after_rejected_commit(self, commit_error: BaseException) -> None:
        # SQLite leaves the transaction open after a rejected COMMIT (a deferred foreign key
        # violation) - it must be rolled back explicitly.
        try:
            await self._connection.rollback()
        except Exception as rollback_error:
            commit_error.add_note(f"ROLLBACK after the rejected COMMIT also failed: {rollback_error!r}")
