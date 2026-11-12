from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

import asyncpg
from asyncpg.transaction import Transaction

from hare.dialects.base.client.enums import StatementForm
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.client.transaction_lifecycle.pending_statements import PendingStatements
from hare.dialects.base.transactions.savepoints.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.dialects.postgresql.client.postgresql_transaction_client import PostgresqlTransactionClient
from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient
from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_parameters import AsyncpgParameters
from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_transaction_connection_wrapper import (
    AsyncpgTransactionConnectionWrapper,
)
from hare.dialects.postgresql.drivers.asyncpg.constants import (
    DEFAULT_STREAM_PREFETCH,
)
from hare.dialects.postgresql.drivers.constants import (
    POSTGRES_RELEASE_SAVEPOINT_SQL,
    POSTGRES_ROLLBACK_TO_SAVEPOINT_SQL,
    POSTGRES_SAVEPOINT_SQL,
    POSTGRES_TRANSACTION_ALIVE_CHECK_SQL,
)


class AsyncpgTransactionClient(PostgresqlTransactionClient, AsyncpgClient):
    """A transactional connection wrapper for asyncpg.

    The top level runs on asyncpg's own Transaction object; a nested wrapper issues its own
    SAVEPOINT/RELEASE SAVEPOINT/ROLLBACK TO SAVEPOINT, so a savepoint whose RELEASE failed can
    still be rolled back to.
    """

    begins_without_suspending = True

    #: The client the transaction is opened on - the outer transaction's for a savepoint.
    _parent: AsyncpgClient
    _outer_transaction: AsyncpgTransactionClient | None

    def __init__(self, connection: AsyncpgClient, savepoint_lock: NestedSavepointLock | None = None) -> None:
        TransactionClient.__init__(self, connection, savepoint_lock)
        self._connection: asyncpg.Connection = connection._connection
        #: Serializes every command on the one physical connection - one lock per top-level
        #: transaction, shared by its nested wrappers. FIFO, so a long stream isn't starved.
        self._lock: asyncio.Lock = (
            connection._lock if isinstance(connection, AsyncpgTransactionClient) else asyncio.Lock()
        )
        self.schema = connection.schema
        self.transaction: Transaction | None = None
        self.command_timeout = connection.command_timeout
        #: The pool the top-level transaction's connection was checked out of - None until then.
        self._held_pool: Any = None
        self._schema_touched = False

    async def _take_transaction_resources(self) -> None:
        # The connection is checked out before the context publishes this client, so no task
        # sees it without one.
        self._held_pool, self._connection = await self._parent.acquire_from_open_pool()

    async def _undo_failed_begin(self) -> None:
        # The BEGIN may have landed though begin() raised (a cancellation) - rolled back here,
        # not by the pool's reset, which logs it as an error.
        if self._connection.is_in_transaction():
            await self.rollback()

    async def _give_back_transaction_resources(self) -> None:
        if self._held_pool is None:
            return
        if self._schema_touched:
            # Other connections may have refreshed while this DDL was still uncommitted.
            self._parent.schema_generation += 1
            self._schema_touched = False
        if self._prepared_without_release and self.transaction is not None:
            # PREPARE TRANSACTION (Transactions.distributed()) ended the transaction without asyncpg's
            # commit()/rollback(): asyncpg still holds it as the connection's open one, and the pool's
            # reset would log an error before its ROLLBACK - the same ROLLBACK, a no-op now, sent here.
            self._prepared_without_release = False
            await self.transaction.rollback()
        if self._shielded_operation_abandoned:
            # A shielded COMMIT/ROLLBACK given up on may still use the connection - closed at once,
            # so the pool discards it instead of handing it to another caller.
            self._connection.terminate()
        await self._parent._pool_release(self._held_pool, self._connection)

    def acquire_connection(self) -> AsyncpgTransactionConnectionWrapper:
        return AsyncpgTransactionConnectionWrapper(self)

    async def invalidate_schema_cache(self) -> None:
        """Refresh this transaction and mark its catalog changes for publication at its end."""
        transaction = self._outer_transaction or self
        transaction._schema_touched = True
        transaction._parent.schema_generation += 1
        try:
            async with self.acquire_connection():
                pass
        except asyncpg.InFailedSQLTransactionError:
            # Rollback control statements bypass refresh. The next ordinary statement
            # refreshes after rollback; the original schema error remains the cause.
            pass

    def get_connection_pool(self) -> asyncpg.Pool | None:
        return self._parent.get_connection_pool()

    async def _driver_send_begin(self) -> None:
        await self.transaction.start()  # type: ignore[union-attr]

    @PostgresqlClient.translate_exceptions(StatementForm.MANY)
    async def execute_many(self, query: str, values: list[Any]) -> None:
        async with self.acquire_connection() as connection:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug("%s: %s", query, values)
            await AsyncpgClient.run_ending_connection_on_cancel(
                connection, connection.executemany(query, [AsyncpgParameters.get_bound_values(row) for row in values])
            )

    async def _driver_stream_batches(
        self, query: str, values: list[Any] | None = None, chunk_size: int = 0
    ) -> AsyncGenerator[list[asyncpg.Record]]:
        """Streams the rows of ``query`` off a server-side cursor of this transaction, a batch of
        ``chunk_size`` rows per round trip. Cursor opening and each fetch hold a query span and
        the connection lock, never across a ``yield``. Sibling savepoints cannot capture these
        statements. The shared statement boundary translates errors.
        """
        self._check_statement_allowed()
        values = values or []
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("%s: %s", query, values)
        batch_size = chunk_size or DEFAULT_STREAM_PREFETCH
        async with self.acquire_connection() as connection:
            cursor = await connection.cursor(query, *AsyncpgParameters.get_bound_values(values))
        while True:
            async with self.acquire_connection():
                records = await cursor.fetch(batch_size)
            if not records:
                return
            yield records

    async def _driver_begin(self) -> None:
        # The BEGIN goes out with the transaction's first statement (_driver_send_begin()).
        self.transaction = self._connection.transaction()
        self._begin_pending = True

    def _has_begun(self) -> bool:
        return self.transaction is not None

    async def _driver_commit(self) -> None:
        async with self._lock:
            await self.transaction.commit()  # type: ignore[union-attr]

    async def _driver_rollback(self) -> None:
        async with self._lock:
            await self.transaction.rollback()  # type: ignore[union-attr]

    async def _driver_savepoint(self, name: str) -> None:
        # Sent ahead of a statement (PendingStatements.send_pending_savepoints()), the connection lock already held.
        await PendingStatements.send_pending_begin(self)
        await self._connection.execute(POSTGRES_SAVEPOINT_SQL.format(name=name))

    async def _driver_release_savepoint(self, name: str) -> None:
        await self._execute_locked(POSTGRES_RELEASE_SAVEPOINT_SQL.format(name=name))

    async def _driver_rollback_to_savepoint(self, name: str) -> None:
        await self._execute_locked(POSTGRES_ROLLBACK_TO_SAVEPOINT_SQL.format(name=name))
        transaction = self._outer_transaction or self
        if transaction._schema_touched:
            transaction._parent.schema_generation += 1

    async def _driver_check_alive(self) -> None:
        await self._execute_locked(POSTGRES_TRANSACTION_ALIVE_CHECK_SQL)

    async def _execute_locked(self, sql: str) -> None:
        """Runs one command directly on the connection, holding the transaction's connection lock.

        Args:
            sql: The SQL command.
        """
        async with self._lock:
            await PendingStatements.send_pending_begin(self)
            await self._connection.execute(sql)

    def _is_aborted_error(self, error: BaseException) -> bool:
        return isinstance(error, asyncpg.InFailedSQLTransactionError)

    def _is_connection_lost(self, error: BaseException) -> bool:
        # asyncpg raises InterfaceError before sending anything on a closed connection.
        return isinstance(
            error,
            (
                asyncpg.exceptions.PostgresConnectionError,
                asyncpg.InterfaceError,
                asyncpg.exceptions.InternalClientError,
            ),
        )

    def _is_commit_outcome_unknown(self, error: BaseException) -> bool:
        return isinstance(error, asyncpg.exceptions.PostgresConnectionError)

    def _is_commit_rejection(self, error: BaseException) -> bool:
        return isinstance(error, asyncpg.PostgresError)
