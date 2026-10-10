from __future__ import annotations

import abc
import asyncio
import time
from itertools import count
from typing import cast

from hare.dialects.base.client.constants import TRANSACTION_FINALISED_MESSAGE
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.transactions.savepoints.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.sqlite.client.sqlite_client import SqliteClient
from hare.dialects.sqlite.constants import SQLITE_TRANSACTION_ABORTED_MESSAGE
from hare.dialects.sqlite.transactions.constants import SQLITE_DISABLE_QUERY_ONLY_SQL
from hare.exceptions import TransactionManagementError
from hare.instrumentation.pools.pool_metrics import PoolMetrics
from hare.transactions.enums import TransactionEventType


class SqliteTransactionClient(TransactionClient, SqliteClient):
    """What every SQLite driver's transaction shares: the client's one connection held for the
    top-level transaction, its busy timeout for a lock timeout, the transaction SQLite rolled back
    on its own, a read-only transaction and the savepoint names. A driver's transaction client
    sends the statements."""

    #: One counter for the savepoint names of every connection.
    _savepoint_name_counter = count()
    #: The client the transaction is opened on - the outer transaction's for a savepoint.
    _parent: SqliteClient

    def __init__(self, connection: SqliteClient, savepoint_lock: NestedSavepointLock | None = None) -> None:
        TransactionClient.__init__(self, connection, savepoint_lock)
        self._connection = connection._connection
        self.binds_parameters_by_number = connection.binds_parameters_by_number
        self._lock = asyncio.Lock()
        #: Whether the transaction is aborted - read on the top-level wrapper, shared by every nested
        #: savepoint wrapper of the same transaction.
        self._transaction_aborted = False
        #: The connection's busy timeout before a top-level transaction with a lock timeout set it.
        self._previous_busy_timeout: int | None = None

    async def _take_transaction_resources(self) -> None:
        # The one connection belongs to one top-level transaction at a time.
        parent = self._parent
        if parent.is_transaction_client:
            await parent._lock.acquire()
        else:
            # The client's one connection, taken like a pool's - counted in place, without a call.
            statistics = parent.pool_statistics
            statistics.acquiring += 1
            start_time = time.perf_counter() if PoolMetrics.enabled else 0.0
            try:
                await parent._lock.acquire()
            finally:
                statistics.acquiring -= 1
            statistics.acquire_count += 1
            if start_time:
                statistics.add_wait(time.perf_counter() - start_time)
        parent._transaction_task = asyncio.current_task()
        try:
            if not self._connection:
                await parent.create_connection(with_db=True)
                self._connection = parent._connection
            lock_timeout = self._transaction_options.lock_timeout
            if lock_timeout is not None:
                self._previous_busy_timeout = await self.swap_busy_timeout(
                    self._connection, max(1, round(lock_timeout * 1000))
                )
        except BaseException:
            await self._give_back_transaction_resources()
            raise

    async def _give_back_transaction_resources(self) -> None:
        parent = self._parent
        try:
            if self._previous_busy_timeout is not None:
                previous_busy_timeout, self._previous_busy_timeout = self._previous_busy_timeout, None
                await self.swap_busy_timeout(self._connection, previous_busy_timeout)
        finally:
            parent._transaction_task = None
            parent._lock.release()

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
        await self._execute_on_aborted_transaction(SQLITE_DISABLE_QUERY_ONLY_SQL)

    @abc.abstractmethod
    async def _execute_on_aborted_transaction(self, query: str) -> None:
        """Runs a statement on the connection of a transaction SQLite already rolled back - past the
        check refusing statements of an aborted transaction.

        Args:
            query: The statement.
        """

    savepoint = SqliteClient.translate_exceptions(TransactionClient.savepoint)
    commit = SqliteClient.translate_exceptions(TransactionClient.commit)
    rollback = SqliteClient.translate_exceptions(TransactionClient.rollback)

    async def begin(self) -> None:
        try:
            await super().begin()
        except self.driver_errors.operational as error:  # pragma: nocoverage
            raise TransactionManagementError(error) from error

    async def _driver_begin(self) -> None:
        # The BEGIN goes out ahead of the transaction's first statement (_driver_send_begin()).
        self._begin_pending = True

    def _is_commit_rejection(self, error: BaseException) -> bool:
        return isinstance(error, Exception)

    async def _check_commit_allowed(self) -> None:
        if self._finalized:
            raise TransactionManagementError(TRANSACTION_FINALISED_MESSAGE)
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

    def _get_new_savepoint_name(self) -> str:
        return self._gen_savepoint_name()

    @classmethod
    def _gen_savepoint_name(cls) -> str:
        return f"hare_savepoint_{next(cls._savepoint_name_counter)}"
