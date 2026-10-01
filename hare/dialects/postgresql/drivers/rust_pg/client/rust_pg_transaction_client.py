from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.base.savepoint_span import current_savepoint_span
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.dialects.postgresql.client.postgresql_transaction_client import PostgresqlTransactionClient
from hare.dialects.postgresql.constants import (
    POSTGRES_TRANSACTION_ALIVE_CHECK_SQL,
)
from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_client import RustPgClient
from hare.dialects.postgresql.drivers.rust_pg.constants import RUST_PG_DEFAULT_STREAM_BATCH_SIZE
from hare.exceptions import (
    DBConnectionError,
    IntegrityError,
    OperationalError,
    TransactionManagementError,
    UnSupportedError,
)
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
        self._tx: pg.Transaction | None = getattr(connection, "_tx", None)
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
        if self._tx is not None and not self._finalized:
            # The COMMIT/ROLLBACK itself raised - rolled back for real before the block exits, so
            # a DROP DATABASE right after (a test teardown) doesn't race the drop guard's
            # background rollback.
            await self._roll_back_unfinished_transaction()
        elif self._tx is not None and self._prepared_without_release:
            # PREPARE TRANSACTION (Transactions.distributed()) ended the transaction without
            # pg.Transaction's commit()/rollback() - the pinned connection is given back with no
            # SQL sent.
            await self._tx.finish_prepared()
            self._prepared_without_release = False

    async def _roll_back_unfinished_transaction(self) -> None:
        """Rolls the driver's transaction back when it was begun and not finished."""
        if self._tx is None or self._finalized:
            return
        try:
            await self._tx.rollback()
        except pg.QueryError, pg.TransactionFinishedError, pg.ConnectionError:
            # Finished by the interrupted COMMIT/ROLLBACK itself, or ended with the lost
            # connection - nothing left to roll back.
            pass
        self._finalized = True

    async def _acquire_transaction_slot(self) -> None:
        """Waits for one of the pool's transaction slots, bounded by ``pool_acquire_timeout``.

        Raises:
            DBConnectionError: if no slot became free within ``pool_acquire_timeout``.
        """
        slots = self._parent._transaction_slots
        if slots is None:
            return
        try:
            if slots.locked():
                await asyncio.wait_for(slots.acquire(), self._parent.pool_acquire_timeout)
            else:
                # A free slot is taken without suspending - no timeout to arm.
                await slots.acquire()
        except TimeoutError:
            raise DBConnectionError(
                f"Timed out after {self._parent.pool_acquire_timeout}s waiting for a free pooled connection"
            ) from None
        self._held_transaction_slots = slots

    async def _release_transaction_slot(self) -> None:
        """Gives the transaction slot back, once - a no-op when none is held."""
        slots, self._held_transaction_slots = self._held_transaction_slots, None
        if slots is not None:
            slots.release()

    @property
    def _active_tx(self) -> pg.Transaction:
        """The open ``pg.Transaction`` - the methods ending a transaction check for None themselves and
        raise ``TransactionManagementError``.
        """
        assert self._tx is not None, "transaction method called before begin()/savepoint()"  # nosec B101
        return self._tx

    async def _run_under_savepoint_span(self, make_coroutine: Callable[[], Awaitable[Any]]) -> Any:
        """Runs a query under a transient span of the savepoint lock, for the duration of the query - a
        query never lands inside a sibling's savepoint. The wait is bounded.

        Takes a callable, not an awaitable: the driver's async methods start the query the moment
        they are called, so the call is made only once the span is taken.
        """
        savepoint_lock = self._savepoint_lock
        current_span = current_savepoint_span.get()
        span = savepoint_lock.open_without_waiting(current_span)
        if span is None:
            span = await savepoint_lock.wait_for_query_span(current_span)
        try:
            # The transaction may have started ending (or ended) while this query waited.
            self._check_statement_allowed()
            if (self._outer_transaction or self)._pending_savepoints:
                await self._send_pending_savepoints()
            return await make_coroutine()
        finally:
            savepoint_lock.release(span)

    async def _run_statement(self, make_call: Callable[[Any], Awaitable[Any]]) -> Any:
        # On the transaction's own pinned connection, under a savepoint span of its own -
        # _run_under_savepoint_span() without its calls, on the path of every statement. Every
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
                await self._send_pending_savepoints()
            return await make_call(self._tx)
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
            await self._send_pending_savepoints()
        values = values or []
        self.log.debug("%s: %s", query, values)
        batch_size = chunk_size or RUST_PG_DEFAULT_STREAM_BATCH_SIZE
        row_stream = await self._run_shielded_from_cancellation(
            self._active_tx.stream(query, values), on_landed=lambda: None
        )
        try:
            while True:
                rows = await self._run_shielded_from_cancellation(
                    row_stream.fetch_many(batch_size), on_landed=lambda: None
                )
                if not rows:
                    return
                yield rows
        except pg.IntegrityViolationError as exc:
            self._mark_statement_failed()
            raise IntegrityError(exc) from exc
        except (pg.InvalidTransactionStateError, pg.TransactionFinishedError) as exc:
            self._mark_statement_failed()
            raise TransactionManagementError(exc) from exc
        except pg.ConnectionError as exc:
            self._mark_statement_failed()
            raise self.get_connection_error_translation(exc, (values,)) from exc
        except (pg.QueryError, pg.ConversionError) as exc:
            self._mark_statement_failed()
            raise OperationalError(exc) from exc
        except asyncio.CancelledError:
            self._mark_statement_failed()
            raise
        finally:
            row_stream.close()
            del row_stream

    async def copy(
        self, table: str, columns: list[str], records: list[tuple[Any, ...]], column_types: list[str]
    ) -> None:
        # The driver has no COPY inside a transaction - the inherited copy() would run and commit on
        # another connection, outside this transaction.
        raise UnSupportedError(
            "bulk_create(use_copy=True) is not supported on rust_pg inside an active "
            "Transactions.atomic() block - rust.native.pg's COPY protocol support has no "
            "transaction-participating variant yet. Call it outside the transaction, or use "
            "the default multi-row INSERT path (use_copy=False) instead."
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
            if self._tx is None:
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
        self._tx = self._parent._connected_pool.begin_on(self._begin_connection, None)

    def _has_begun(self) -> bool:
        return self._tx is not None

    async def _driver_commit(self) -> None:
        # A transaction no statement ran in ends without suspending - the COMMIT needs no shield.
        transaction = self._active_tx
        if not transaction.end_unbegun():
            await transaction.commit()

    async def _driver_rollback(self) -> None:
        transaction = self._active_tx
        if not transaction.end_unbegun():
            await transaction.rollback()

    async def _driver_savepoint(self, name: str) -> None:
        await self._active_tx.savepoint(name)

    async def _driver_release_savepoint(self, name: str) -> None:
        await self._active_tx.release(name)

    async def _driver_rollback_to_savepoint(self, name: str) -> None:
        await self._active_tx.rollback_to(name)

    async def _driver_check_alive(self) -> None:
        await self._active_tx.execute_script(POSTGRES_TRANSACTION_ALIVE_CHECK_SQL)

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
