from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from hare.dialects.base.client.transaction_lifecycle.pending_statements import PendingStatements
from hare.dialects.base.transactions.savepoints.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.base.transactions.savepoints.savepoint_span import SavepointSpan, current_savepoint_span
from hare.exceptions import (
    TransactionManagementError,
)
from hare.instrumentation.pools.pool_metrics import PoolMetrics

if TYPE_CHECKING:
    from types import TracebackType

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient

TConnection = TypeVar("TConnection")  # Instance of client connection, such as: asyncpg.Connection()


class ConnectionWrapper(Generic[TConnection]):
    """Wraps the connections with a lock to facilitate safe concurrent access when using
    asyncio.gather, TaskGroup, or similar."""

    __slots__ = ("connection", "_lock", "client", "_span")

    def __init__(self, lock: asyncio.Lock, client: DatabaseClient) -> None:
        self._lock: asyncio.Lock = lock
        self.client = client
        self.connection: TConnection = client._connection
        self._span: SavepointSpan | None = None

    async def ensure_connection(self) -> None:
        if not self.connection:
            await self.client.create_connection(with_db=True)
            self.connection = self.client._connection

    async def _acquire_savepoint_span(self, savepoint_lock: NestedSavepointLock) -> None:
        # A query on a transaction takes a transient span of the savepoint lock for its own duration
        # - it never lands between a sibling task's SAVEPOINT and its ROLLBACK TO, which would undo
        # it too. A query at the top level waits for every open savepoint below the transaction; one
        # inside a savepoint only for its own descendants. The wait is bounded and raises
        # TransactionManagementError; a span of another connection's lock is filtered out.
        current_span = current_savepoint_span.get()
        span = savepoint_lock.open_without_waiting(current_span)
        if span is None:
            span = await savepoint_lock.wait_for_query_span(current_span)
        self._span = span
        # The transaction may have started ending (or ended) while this query waited for its turn.
        try:
            cast("TransactionClient", self.client)._check_statement_allowed()
        except TransactionManagementError:
            savepoint_lock.release(self._span)
            self._span = None
            raise

    async def __aenter__(self) -> TConnection:
        client = self.client
        is_transaction_client = client.is_transaction_client
        savepoint_lock = getattr(client, "_savepoint_lock", None) if is_transaction_client else None
        if savepoint_lock is not None:
            await self._acquire_savepoint_span(savepoint_lock)
        if is_transaction_client:
            await self._lock.acquire()
        else:
            # The client's one connection, taken like a pool's - counted in place, without a call.
            statistics = client.pool_statistics
            statistics.acquiring += 1
            start_time = time.perf_counter() if PoolMetrics.enabled else 0.0
            try:
                await self._lock.acquire()
            finally:
                statistics.acquiring -= 1
            statistics.acquire_count += 1
            if start_time:
                statistics.add_wait(time.perf_counter() - start_time)
        try:
            await self.ensure_connection()
        except BaseException:
            # __aexit__ never runs if __aenter__ itself raises (async-context-manager
            # protocol) - without this, a failed create_connection() would permanently hold
            # the lock, wedging every future acquire_connection() on this client.
            self._lock.release()
            if savepoint_lock is not None and self._span is not None:
                savepoint_lock.release(self._span)
                self._span = None
            raise
        if is_transaction_client:
            # The transaction's BEGIN and the SAVEPOINTs still pending go ahead of the statement.
            transaction_client: TransactionClient = client  # type: ignore[assignment]  # cast() is a call per statement
            transaction = transaction_client._outer_transaction or transaction_client
            if transaction._begin_pending or transaction._pending_savepoints:
                await self._send_pending_statements()
        return self.connection

    async def _send_pending_statements(self) -> None:
        """Sends the BEGIN and the SAVEPOINTs the transaction still holds back, ahead of the
        statement this connection was taken for - the connection is given back when that fails."""
        try:
            await PendingStatements.send_pending_transaction_statements(cast("TransactionClient", self.client))
        except BaseException as error:
            await self.__aexit__(type(error), error, error.__traceback__)
            raise

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        self._lock.release()
        # A span is taken only on a transaction client's savepoint lock.
        if self._span is not None:
            cast("TransactionClient", self.client)._savepoint_lock.release(self._span)
            self._span = None
