from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from hare.dialects.base.client.pool.pool_timeouts import PoolTimeouts
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.client.transaction_lifecycle.pending_statements import PendingStatements
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.dialects.base.transactions.savepoints.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.base.transactions.savepoints.savepoint_span import current_savepoint_span
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.dialects.postgresql.client.postgresql_transaction_client import PostgresqlTransactionClient
from hare.dialects.postgresql.drivers.constants import POSTGRES_TRANSACTION_ALIVE_CHECK_SQL
from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_client import RustPgClient
from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_transaction_connection_wrapper import (
    RustPgTransactionConnectionWrapper,
)
from hare.dialects.postgresql.drivers.rust_pg.constants import RUST_PG_DEFAULT_STREAM_BATCH_SIZE
from hare.exceptions import (
    IntegrityError,
    OperationalError,
    TransactionManagementError,
)
from hare.instrumentation.pools.pool_metrics import PoolMetrics
from rust.native import pg


class RustPgTransactionClient(PostgresqlTransactionClient, RustPgClient):
    """A transaction of the Rust driver. The driver's ``pg.Transaction`` doesn't nest: the outermost
    client owns it (``begin()`` creates it), a nested one shares it and issues the savepoint
    statements under a name of its own.
    """

    #: The client the transaction is opened on - the outer transaction's for a savepoint.
    _parent: RustPgClient

    def __init__(self, connection: RustPgClient, savepoint_lock: NestedSavepointLock | None = None) -> None:
        TransactionClient.__init__(self, connection, savepoint_lock)
        self._pool = connection._pool
        self._native_transaction: pg.Transaction | None = getattr(connection, "_native_transaction", None)
        self.command_timeout = connection.command_timeout
        self._begin_connection: Any = None
        self._held_transaction_slots: asyncio.Semaphore | None = None

    async def _take_transaction_resources(self) -> None:
        # The transaction slot itself is taken by begin(), right before the connection.
        await self._parent._ensure_connection()

    async def _undo_failed_begin(self) -> None:
        # A BEGIN that landed though begin() raised (a cancellation) left a pg.Transaction owning
        # a pooled connection until it is committed or rolled back - its Rust-side drop guard only
        # spawns an unordered background rollback.
        await self._roll_back_unfinished_transaction()

    async def _give_back_transaction_resources(self) -> None:
        await self._release_transaction_slot()

    async def _end_unfinished_transaction(self) -> None:
        if self._native_transaction is not None and not self._finalized:
            # The COMMIT/ROLLBACK itself raised - rolled back for real before the block exits, so
            # a DROP DATABASE right after (a test teardown) doesn't race the drop guard's
            # background rollback.
            await self._roll_back_unfinished_transaction()
        elif self._native_transaction is not None and self._prepared_without_release:
            # PREPARE TRANSACTION (Transactions.distributed()) ended the transaction without
            # pg.Transaction's commit()/rollback() - the pinned connection is given back with no
            # SQL sent.
            await self._native_transaction.finish_prepared()
            self._prepared_without_release = False

    async def _roll_back_unfinished_transaction(self) -> None:
        """Rolls the driver's transaction back when it was begun and not finished."""
        if self._native_transaction is None or self._finalized:
            return
        try:
            await self._native_transaction.rollback()
        except (pg.QueryError, pg.TransactionFinishedError, pg.ConnectionError):
            # Finished by the interrupted COMMIT/ROLLBACK itself, or ended with the lost
            # connection - nothing left to roll back.
            pass
        self._finalized = True

    async def _acquire_transaction_slot(self) -> None:
        """Waits for one of the pool's transaction slots, bounded by ``pool_acquire_timeout``.

        Raises:
            DBConnectionError: if no slot became free within ``pool_acquire_timeout``.
        """
        parent = self._parent
        slots = parent._transaction_slots
        if slots is None:
            return
        if not slots.locked():
            # A free slot is taken without suspending - no timeout to arm, nothing waited.
            await slots.acquire()
            self._held_transaction_slots = slots
            return
        parent.transaction_slot_waiting += 1
        start_time = time.perf_counter() if PoolMetrics.enabled else 0.0
        try:
            await asyncio.wait_for(slots.acquire(), parent.pool_acquire_timeout)
        except TimeoutError:
            raise PoolTimeouts.get_error(parent, parent.pool_acquire_timeout or 0.0) from None
        finally:
            parent.transaction_slot_waiting -= 1
        if start_time:
            parent.pool_statistics.add_wait(time.perf_counter() - start_time)
        self._held_transaction_slots = slots

    async def _release_transaction_slot(self) -> None:
        """Gives the transaction slot back, once - a no-op when none is held."""
        slots, self._held_transaction_slots = self._held_transaction_slots, None
        if slots is not None:
            slots.release()

    def acquire_connection(self) -> RustPgTransactionConnectionWrapper:
        return RustPgTransactionConnectionWrapper(self)

    @property
    def _active_native_transaction(self) -> pg.Transaction:
        """The open ``pg.Transaction`` - the methods ending a transaction check for None themselves and
        raise ``TransactionManagementError``.
        """
        assert self._native_transaction is not None, "transaction method called before begin()/savepoint()"  # nosec B101
        return self._native_transaction

    async def _run_statement(self, make_call: Callable[[Any], Awaitable[Any]]) -> Any:
        # On the transaction's own pinned connection, under a savepoint span of its own - a query
        # never lands inside a sibling's savepoint, and the wait is bounded. Every
        # caller checked the statement is allowed right before, so it is checked again only after
        # a wait for the span.
        savepoint_lock = self._savepoint_lock
        current_span = current_savepoint_span.get()
        span = savepoint_lock.open_without_waiting(current_span)
        if span is None:
            span = await savepoint_lock.wait_for_query_span(current_span)
            try:
                self._check_statement_allowed()
            except BaseException:
                savepoint_lock.release(span)
                raise
        try:
            if (self._outer_transaction or self)._pending_savepoints:
                await PendingStatements.send_pending_savepoints(self)
            return await make_call(self._native_transaction)
        finally:
            savepoint_lock.release(span)

    async def _driver_stream_batches(
        self, query: str, values: list[Any] | None = None, chunk_size: int = 0
    ) -> AsyncGenerator[list[Any]]:
        """Streams the rows of ``query`` off a portal, a batch of ``chunk_size`` rows per fetch
        (``RUST_PG_DEFAULT_STREAM_BATCH_SIZE`` when 0). Driver errors are translated here.

        Opening the stream and each fetch are shielded from cancellation one by one: a cancelled
        Rust future left the connection wedged for the transaction's own ROLLBACK/COMMIT. However
        the generator ends, the row stream is closed at once - an unread portal blocks every later
        statement on the connection.
        """
        self._check_statement_allowed()
        if (self._outer_transaction or self)._pending_savepoints:
            await PendingStatements.send_pending_savepoints(self)
        values = values or []
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("%s: %s", query, values)
        batch_size = chunk_size or RUST_PG_DEFAULT_STREAM_BATCH_SIZE
        row_stream = await TransactionEnding.run_shielded_from_cancellation(
            self._active_native_transaction.stream(query, values), on_landed=lambda: None
        )
        try:
            while True:
                rows = await TransactionEnding.run_shielded_from_cancellation(
                    row_stream.fetch_many(batch_size), on_landed=lambda: None
                )
                if not rows:
                    return
                yield rows
        except pg.IntegrityViolationError as error:
            self._mark_statement_failed()
            raise IntegrityError(error) from error
        except (pg.InvalidTransactionStateError, pg.TransactionFinishedError) as error:
            self._mark_statement_failed()
            raise TransactionManagementError(error) from error
        except pg.ConnectionError as error:
            self._mark_statement_failed()
            raise self.get_connection_error_translation(error, (values,)) from error
        except (pg.QueryError, pg.ConversionError) as error:
            self._mark_statement_failed()
            raise OperationalError(error) from error
        except asyncio.CancelledError:
            self._mark_statement_failed()
            raise
        finally:
            row_stream.close()
            del row_stream

    @PostgresqlClient.translate_exceptions
    async def copy(
        self, table: str, columns: list[str], records: list[tuple[Any, ...]], column_types: list[str]
    ) -> None:
        """Loads ``records`` into ``table`` through the COPY protocol on the transaction's own
        connection - the rows commit or roll back with the transaction (a savepoint's rollback
        included)."""
        self.log.debug("COPY %s(%s): %d record(s)", table, columns, len(records))
        await self._run_statement(
            lambda executor: executor.copy_in(table, columns, column_types, [list(row) for row in records])
        )

    @PostgresqlClient.translate_exceptions
    async def begin(self) -> None:
        # Waiting for a pool connection is cancellable - nothing is sent. Neither is anything by
        # begin_on(): the BEGIN goes out with the transaction's first statement, so there is nothing
        # to shield from a cancellation.
        await self._acquire_transaction_slot()
        self._begin_connection = None
        try:
            self._begin_connection = await self._parent._pool_acquire()
            await self._driver_begin()
        except BaseException:
            if self._native_transaction is None:
                try:
                    if self._begin_connection is not None:
                        # A no-op once begin_on() has taken the connection over.
                        await self._parent._connected_pool.release(self._begin_connection)
                finally:
                    await self._release_transaction_slot()
            raise
        finally:
            self._begin_connection = None

    async def _driver_begin(self) -> None:
        self._native_transaction = self._parent._connected_pool.begin_on(self._begin_connection, None)

    def _has_begun(self) -> bool:
        return self._native_transaction is not None

    async def _driver_commit(self) -> None:
        # A transaction no statement ran in ends without suspending - the COMMIT needs no shield.
        transaction = self._active_native_transaction
        if not transaction.end_unbegun():
            await transaction.commit()

    async def _driver_rollback(self) -> None:
        transaction = self._active_native_transaction
        if not transaction.end_unbegun():
            await transaction.rollback()

    async def _driver_savepoint(self, name: str) -> None:
        await self._active_native_transaction.savepoint(name)

    async def _driver_release_savepoint(self, name: str) -> None:
        await self._active_native_transaction.release(name)

    async def _driver_rollback_to_savepoint(self, name: str) -> None:
        await self._active_native_transaction.rollback_to(name)

    async def _driver_check_alive(self) -> None:
        await self._active_native_transaction.execute_script(POSTGRES_TRANSACTION_ALIVE_CHECK_SQL)

    def _is_aborted_error(self, error: BaseException) -> bool:
        return isinstance(error, pg.InvalidTransactionStateError)

    def _is_connection_lost(self, error: BaseException) -> bool:
        return isinstance(error, pg.ConnectionError)

    def _is_commit_outcome_unknown(self, error: BaseException) -> bool:
        # ConnectionClosedError means nothing was sent.
        return not isinstance(error, pg.ConnectionClosedError)

    def _is_commit_rejection(self, error: BaseException) -> bool:
        return isinstance(error, pg.QueryError)

    def _is_transaction_finished_error(self, error: BaseException) -> bool:
        # COMMIT/ROLLBACK take the connection out of the driver's Transaction - a later call on it
        # reports that an earlier, interrupted one already ended the transaction.
        return isinstance(error, pg.TransactionFinishedError)
