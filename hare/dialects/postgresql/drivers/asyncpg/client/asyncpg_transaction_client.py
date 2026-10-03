import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast

import asyncpg
from asyncpg.transaction import Transaction

from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.dialects.postgresql.client.postgresql_transaction_client import PostgresqlTransactionClient
from hare.dialects.postgresql.constants import (
    POSTGRES_RELEASE_SAVEPOINT_SQL,
    POSTGRES_ROLLBACK_TO_SAVEPOINT_SQL,
    POSTGRES_SAVEPOINT_SQL,
    POSTGRES_TRANSACTION_ALIVE_CHECK_SQL,
)
from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient
from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_transaction_connection_wrapper import (
    AsyncpgTransactionConnectionWrapper,
)
from hare.dialects.postgresql.drivers.asyncpg.constants import (
    DEFAULT_STREAM_PREFETCH,
)
from hare.exceptions import (
    DBConnectionError,
    IntegrityError,
    OperationalError,
)


class AsyncpgTransactionClient(PostgresqlTransactionClient, AsyncpgClient):
    """A transactional connection wrapper for asyncpg.

    The top level runs on asyncpg's own Transaction object; a nested wrapper issues its own
    SAVEPOINT/RELEASE SAVEPOINT/ROLLBACK TO SAVEPOINT, so a savepoint whose RELEASE failed can
    still be rolled back to.
    """

    #: The client the transaction is opened on - the outer transaction's for a savepoint.
    _parent: AsyncpgClient

    def __init__(self, connection: AsyncpgClient, savepoint_lock: NestedSavepointLock | None = None) -> None:
        TransactionClient.__init__(self, connection, savepoint_lock)
        self._connection: asyncpg.Connection = connection._connection
        #: Serializes every command on the one physical connection - one lock per top-level
        #: transaction, shared by its nested wrappers. FIFO, so a long stream isn't starved.
        self._lock: asyncio.Lock = (
            connection._lock if isinstance(connection, AsyncpgTransactionClient) else asyncio.Lock()
        )
        # copy() (inherited unchanged) reads it to qualify the COPY target table.
        self.schema = connection.schema
        self.transaction: Transaction | None = None
        self.command_timeout = connection.command_timeout
        #: The pool the top-level transaction's connection was checked out of - None until then.
        self._held_pool: Any = None

    async def _take_transaction_resources(self) -> None:
        await self._parent._ensure_connection()
        # The connection is checked out before the context publishes this client, so no task
        # sees it without one.
        self._held_pool = self._parent._pool
        self._connection = await self._parent._pool_acquire()

    async def _undo_failed_begin(self) -> None:
        # The BEGIN may have landed though begin() raised (a cancellation) - rolled back here,
        # not by the pool's reset, which logs it as an error.
        if self._connection.is_in_transaction():
            await self.rollback()

    async def _give_back_transaction_resources(self) -> None:
        if self._held_pool is None:
            return
        if self._shielded_operation_abandoned:
            # A shielded COMMIT/ROLLBACK given up on may still use the connection - closed at once,
            # so the pool discards it instead of handing it to another caller.
            self._connection.terminate()
        await self._parent._pool_release(self._held_pool, self._connection)

    def acquire_connection(self) -> ConnectionWrapper[asyncpg.Connection]:
        return AsyncpgTransactionConnectionWrapper(self._lock, self)

    async def _driver_send_begin(self) -> None:
        await cast("Transaction", self.transaction).start()

    @PostgresqlClient.translate_exceptions
    async def execute_many(self, query: str, values: list[Any]) -> None:
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            await connection.executemany(query, [self._asyncpg_bind_values(row) for row in values])

    async def _driver_stream_batches(
        self, query: str, values: list[Any] | None = None, chunk_size: int = 0
    ) -> AsyncGenerator[list[asyncpg.Record]]:
        """Streams the rows of ``query`` off a server-side cursor of this transaction, a batch of
        ``chunk_size`` rows per round trip. Each fetch holds the transaction's connection lock, never across a
        ``yield`` - the same task may run another query between fetches. Driver errors are
        translated here.
        """
        self._check_statement_allowed()
        values = values or []
        self.log.debug("%s: %s", query, values)
        batch_size = chunk_size or DEFAULT_STREAM_PREFETCH
        cursor_factory = self._connection.cursor(query, *self._asyncpg_bind_values(values))
        try:
            async with self._lock:
                self._check_statement_allowed()
                await self._send_pending_statements()
                cursor = await cursor_factory
            while True:
                async with self._lock:
                    self._check_statement_allowed()
                    records = await cursor.fetch(batch_size)
                if not records:
                    return
                yield records
        except asyncpg.IntegrityConstraintViolationError as exc:
            self._mark_statement_failed()
            raise IntegrityError(exc) from exc
        except (asyncpg.exceptions.PostgresConnectionError, asyncpg.InterfaceError) as exc:
            self._mark_statement_failed()
            raise DBConnectionError(exc) from exc
        except asyncpg.PostgresError as exc:
            self._mark_statement_failed()
            raise OperationalError(exc) from exc
        except asyncio.CancelledError:
            self._mark_statement_failed()
            raise

    async def _driver_begin(self) -> None:
        # The BEGIN goes out with the transaction's first statement (_driver_send_begin()).
        self.transaction = self._connection.transaction()
        self._begin_pending = True

    def _has_begun(self) -> bool:
        return self.transaction is not None

    async def _driver_commit(self) -> None:
        async with self._lock:
            await cast("Transaction", self.transaction).commit()

    async def _driver_rollback(self) -> None:
        async with self._lock:
            await cast("Transaction", self.transaction).rollback()

    async def _driver_savepoint(self, name: str) -> None:
        # Sent ahead of a statement (_send_pending_savepoints()), the connection lock already held.
        await self._send_pending_begin()
        await self._connection.execute(POSTGRES_SAVEPOINT_SQL.format(name=name))

    async def _driver_release_savepoint(self, name: str) -> None:
        await self._execute_locked(POSTGRES_RELEASE_SAVEPOINT_SQL.format(name=name))

    async def _driver_rollback_to_savepoint(self, name: str) -> None:
        await self._execute_locked(POSTGRES_ROLLBACK_TO_SAVEPOINT_SQL.format(name=name))

    async def _driver_check_alive(self) -> None:
        await self._execute_locked(POSTGRES_TRANSACTION_ALIVE_CHECK_SQL)

    async def _execute_locked(self, sql: str) -> None:
        """Runs one command directly on the connection, holding the transaction's connection lock.

        Args:
            sql: The SQL command.
        """
        async with self._lock:
            await self._send_pending_begin()
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
