import asyncio
import sqlite3
from itertools import count
from typing import Any, cast

import aiosqlite

from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.sqlite.client.sqlite_client import SqliteClient
from hare.dialects.sqlite.client.sqlite_statement_timeout_connection_wrapper import (
    SqliteStatementTimeoutConnectionWrapper,
)
from hare.dialects.sqlite.client.sqlite_transaction_connection_wrapper import SqliteTransactionConnectionWrapper
from hare.dialects.sqlite.constants import (
    SQLITE_DISABLE_QUERY_ONLY_SQL,
    SQLITE_TRANSACTION_ABORTED_MESSAGE,
)
from hare.exceptions import (
    TransactionManagementError,
)
from hare.transactions.enums import TransactionEventType


class SqliteTransactionClient(TransactionClient, SqliteClient):
    #: One counter for the savepoint names of every connection.
    _savepoint_name_counter = count()
    #: The client the transaction is opened on - the outer transaction's for a savepoint.
    _parent: SqliteClient

    def __init__(self, connection: SqliteClient, savepoint_lock: NestedSavepointLock | None = None) -> None:
        TransactionClient.__init__(self, connection, savepoint_lock)
        self._connection: aiosqlite.Connection = cast("aiosqlite.Connection", connection._connection)
        self._lock = asyncio.Lock()
        #: Whether the transaction is aborted - read on the top-level wrapper, shared by every nested
        #: savepoint wrapper of the same transaction.
        self._transaction_aborted = False

    async def _take_transaction_resources(self) -> None:
        # The one connection belongs to one top-level transaction at a time.
        parent = self._parent
        await parent._lock.acquire()
        parent._transaction_task = asyncio.current_task()
        try:
            if not self._connection:
                await parent.create_connection(with_db=True)
                self._connection = cast("aiosqlite.Connection", parent._connection)
        except BaseException:
            await self._give_back_transaction_resources()
            raise

    async def _give_back_transaction_resources(self) -> None:
        parent = self._parent
        parent._transaction_task = None
        parent._lock.release()

    def acquire_connection(self) -> ConnectionWrapper[aiosqlite.Connection]:
        statement_timeout = self._transaction_options.statement_timeout
        if statement_timeout is None:
            return SqliteTransactionConnectionWrapper(self._lock, self)
        return SqliteStatementTimeoutConnectionWrapper(self._lock, self, statement_timeout)

    def _mark_transaction_aborted(self) -> None:
        cast("SqliteTransactionClient", self._get_top_level_transaction())._transaction_aborted = True

    def _is_transaction_aborted(self) -> bool:
        return cast("SqliteTransactionClient", self._get_top_level_transaction())._transaction_aborted

    async def _disable_query_only(self) -> None:
        """Turns ``PRAGMA query_only`` back off before a read-only top-level transaction ends, so
        the write refusal never outlives it on the shared connection - the pragma is
        connection-wide, so this runs even for an aborted transaction."""
        if not self._transaction_options.read_only or self._savepoint_name is not None:
            return
        if not self._is_transaction_aborted():
            await self.execute(SQLITE_DISABLE_QUERY_ONLY_SQL)
            return
        async with self.acquire_connection() as connection:
            await connection.execute(SQLITE_DISABLE_QUERY_ONLY_SQL)

    @SqliteClient.translate_exceptions
    async def execute_many(self, query: str, values: list[list[Any]]) -> None:
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            # Already within transaction, so ideal for performance
            await connection.executemany(query, values)

    @SqliteClient.translate_exceptions
    async def execute_script(self, query: str) -> None:
        """Runs the script's statements one by one inside this transaction - sqlite3's own
        executescript() would commit the open transaction before running anything."""
        async with self.acquire_connection() as connection:
            self.log.debug(query)
            await self._run_on_worker_thread(connection, self._execute_statements, self.split_script(query))

    savepoint = SqliteClient.translate_exceptions(TransactionClient.savepoint)
    commit = SqliteClient.translate_exceptions(TransactionClient.commit)
    rollback = SqliteClient.translate_exceptions(TransactionClient.rollback)

    async def begin(self) -> None:
        try:
            await super().begin()
        except sqlite3.OperationalError as exc:  # pragma: nocoverage
            raise TransactionManagementError(exc) from exc

    async def _driver_begin(self) -> None:
        # The BEGIN goes out ahead of the transaction's first statement (_driver_send_begin()).
        self._begin_pending = True

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
        await self._send_pending_begin()
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

    def _is_commit_rejection(self, error: BaseException) -> bool:
        return isinstance(error, Exception)

    async def _check_commit_allowed(self) -> None:
        if self._finalized:
            raise TransactionManagementError("Transaction already finalised")
        if self._is_transaction_aborted():
            if self._savepoint_name is None:
                await self.rollback()
            raise TransactionManagementError(f"{SQLITE_TRANSACTION_ABORTED_MESSAGE} (nothing was committed)")

    def _check_savepoint_allowed(self) -> None:
        if self._is_transaction_aborted():
            raise TransactionManagementError(SQLITE_TRANSACTION_ABORTED_MESSAGE)

    async def _before_top_level_end(self, event: TransactionEventType) -> Exception | None:
        if event is TransactionEventType.COMMIT:
            await self._disable_query_only()
            return None
        try:
            await self._disable_query_only()
        except Exception as error:
            return error
        return None

    async def _after_rejected_commit(self, commit_error: BaseException) -> None:
        # SQLite leaves the transaction open after a rejected COMMIT (a deferred foreign key
        # violation) - it must be rolled back explicitly.
        try:
            await self._connection.rollback()
        except Exception as rollback_error:
            commit_error.add_note(f"ROLLBACK after the rejected COMMIT also failed: {rollback_error!r}")

    def _get_new_savepoint_name(self) -> str:
        return self._gen_savepoint_name()

    @classmethod
    def _gen_savepoint_name(cls) -> str:
        return f"hare_savepoint_{next(cls._savepoint_name_counter)}"
