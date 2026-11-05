from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from hare.dialects.base.client.constants import DISCARDED_CLIENT_MESSAGE
from hare.dialects.base.client.transaction_lifecycle.pending_statements import PendingStatements
from hare.dialects.base.transactions.savepoints.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.base.transactions.savepoints.savepoint_span import SavepointSpan, current_savepoint_span
from hare.exceptions import (
    DBConnectionError,
    TransactionManagementError,
)
from hare.instrumentation.pools.pool_metrics import PoolMetrics

if TYPE_CHECKING:
    from types import TracebackType

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient

TConnection = TypeVar("TConnection")  # A connection object of the driver.


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
        # Read with the lock held - a close() (Connections.reconnect()) that ran while the statement
        # waited for it closed the connection there was when the wrapper was made.
        connection = self.client._connection
        if not connection:
            if self.client.discarded:
                raise DBConnectionError(DISCARDED_CLIENT_MESSAGE.format(alias=self.client.connection_alias))
            await self.client.create_connection(with_db=True)
            connection = self.client._connection
        self.connection = connection

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
        acquired = False
        try:
            if savepoint_lock is not None:
                await self._acquire_savepoint_span(savepoint_lock)
            if is_transaction_client:
                await self._lock.acquire()
                acquired = True
            else:
                # The client's one connection is counted like an acquired pool connection.
                statistics = client.pool_statistics
                statistics.acquiring += 1
                start_time = time.perf_counter() if PoolMetrics.enabled else 0.0
                try:
                    await self._lock.acquire()
                    acquired = True
                finally:
                    statistics.acquiring -= 1
                statistics.acquire_count += 1
                if start_time:
                    statistics.add_wait(time.perf_counter() - start_time)
            await self.ensure_connection()
            if is_transaction_client:
                # Pending BEGIN and SAVEPOINT statements precede the user's statement.
                transaction_client: TransactionClient = client  # type: ignore[assignment]
                transaction = transaction_client._outer_transaction or transaction_client
                if transaction._begin_pending or transaction._pending_savepoints:
                    await PendingStatements.send_pending_transaction_statements(transaction_client)
        except BaseException:
            # __aexit__ does not run when entry fails, including cancellation while waiting
            # for the physical lock after taking a savepoint span.
            if acquired:
                self._lock.release()
            if savepoint_lock is not None and self._span is not None:
                savepoint_lock.release(self._span)
                self._span = None
            raise
        return self.connection

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
