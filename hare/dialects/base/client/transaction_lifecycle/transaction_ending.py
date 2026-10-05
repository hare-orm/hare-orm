from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from hare.dialects.base.client.constants import (
    SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS,
    TRANSACTION_END_WAIT_TIMEOUT_SECONDS,
    TRANSACTION_FINALISED_MESSAGE,
)
from hare.dialects.base.client.transaction_lifecycle.transaction_callbacks import TransactionCallbacks
from hare.dialects.base.transactions.savepoints.savepoint_span import SavepointSpan, current_savepoint_span
from hare.exceptions import TransactionManagementError
from hare.instrumentation.observers.observers import Observers
from hare.transactions.enums import TransactionEventType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.transaction_client import TransactionClient


class TransactionEnding:
    """How a top-level transaction ends: its resources released and its callbacks run, after a rejected
    commit or a lost connection too, held against a cancellation until the end is complete, and the
    end recorded for the observers."""

    @staticmethod
    async def end_top_level(client: TransactionClient, event: TransactionEventType) -> None:
        """Sends the real COMMIT/ROLLBACK of this top-level transaction and completes it: hands
        its resources back, runs the matching callbacks and records the event.

        Args:
            client: The transaction's client.
            event: ``COMMIT`` or ``ROLLBACK``.

        Raises:
            TransactionManagementError: The transaction never began, has ended, or can't commit.
        """
        if not client._has_begun():
            raise TransactionManagementError("Transaction is in invalid state")
        if client._finalized:
            raise TransactionManagementError(TRANSACTION_FINALISED_MESSAGE)
        is_commit = event is TransactionEventType.COMMIT
        previous_pending = client._pending_top_level_operation
        client._pending_top_level_operation = event
        landed_by_this_call = False

        async def end_and_finish() -> None:
            # One shielded coroutine: a cancellation between the statement and the callbacks would
            # land the one and skip the other.
            nonlocal landed_by_this_call
            deferred_error: Exception | None = None
            span, span_token = await TransactionEnding.hold_transaction_for_ending(
                client, give_up_on_timeout=is_commit
            )
            try:
                if client._finalized:
                    raise TransactionManagementError(TRANSACTION_FINALISED_MESSAGE)
                in_flight = False
                try:
                    deferred_error = await client._before_top_level_end(event)
                    in_flight = True
                    if client._begin_pending:
                        # No statement ran - the driver never sent the BEGIN, and has nothing to end.
                        client._begin_pending = False
                    elif is_commit:
                        await client._driver_commit()
                    else:
                        await client._driver_rollback()
                except BaseException as error:
                    if client._is_connection_lost(error):
                        client._pending_top_level_operation = None
                        if not is_commit:
                            # The server rolls back a transaction whose connection is gone.
                            client.log.warning(
                                "Connection lost while rolling back transaction %s", client.connection_alias
                            )
                            landed_by_this_call = True
                            await TransactionEnding.finish_after_lost_connection(
                                client, error, commit_outcome_unknown=False
                            )
                            return
                        await TransactionEnding.finish_after_lost_connection(
                            client,
                            error,
                            commit_outcome_unknown=in_flight and client._is_commit_outcome_unknown(error),
                        )
                    elif is_commit and client._is_commit_rejection(error):
                        client._pending_top_level_operation = None
                        await client._after_rejected_commit(error)
                        await TransactionEnding.finish_after_failed_commit(client, error)
                    raise
                landed_by_this_call = True
                client._finalized = True
                client._pending_top_level_operation = None
            finally:
                TransactionEnding.stop_holding_transaction(client, span, span_token)
            await TransactionEnding.finish_top_level_operation(client, event)
            if deferred_error is not None:
                raise deferred_error

        try:
            await TransactionEnding.run_shielded_from_cancellation(
                end_and_finish(), on_landed=client._do_nothing, on_timeout=client._mark_top_level_operation_abandoned
            )
        except BaseException as error:
            if landed_by_this_call or not client._is_transaction_finished_error(error):
                raise
            # An earlier, interrupted call already ended the transaction - its callbacks run now.
            client._finalized = True
            client._pending_top_level_operation = None
            if previous_pending is not None:
                await TransactionEnding.run_shielded_from_cancellation(
                    TransactionEnding.finish_top_level_operation(client, previous_pending),
                    on_landed=client._do_nothing,
                )

    @staticmethod
    async def release_transaction_resources(client: TransactionClient) -> None:
        """Gives the transaction's connection (or connection lock) back once the top-level
        COMMIT/ROLLBACK landed, before any callback runs. Idempotent.

        Args:
            client: The transaction's client.
        """
        context = (
            client._transaction_context_reference() if client._transaction_context_reference is not None else None
        )
        if context is not None:
            await context._release_resources()

    @staticmethod
    async def finish_top_level_operation(
        client: TransactionClient,
        event: TransactionEventType,
        *,
        run_callbacks: bool = True,
        cause: Exception | None = None,
    ) -> None:
        """Completes a real top-level COMMIT/ROLLBACK that has just landed: hands the
        transaction's resources back, runs the matching callbacks as the outer level and records
        the instrumentation event whatever the callbacks did.

        Args:
            client: The transaction's client.
            event: ``COMMIT`` or ``ROLLBACK``.
            run_callbacks: False discards every callback without running it - for a transaction
                whose outcome is unknown.
            cause: the error that ended the transaction, reported in its ``TransactionEvent``.
        Raises:
            Exception: the single callback failure, or an ExceptionGroup of several.
        """
        try:
            try:
                await TransactionEnding.release_transaction_resources(client)
            except Exception as release_error:
                client._release_failure = release_error
            # A real, top-level commit means nothing was ever undone - the on_rollback() callbacks
            # are moot and discarded, never fired (and the other way round for a rollback).
            commit_entries, client._on_commit_callbacks = client._on_commit_callbacks, []
            rollback_entries, client._on_rollback_callbacks = client._on_rollback_callbacks, []
            if not run_callbacks:
                return
            if event is TransactionEventType.COMMIT:
                if commit_entries:
                    await TransactionCallbacks.run_callbacks_as_outer_level(
                        client, [entry.callback for entry in commit_entries], "on_commit()"
                    )
            elif rollback_entries:
                await TransactionCallbacks.run_callbacks_as_outer_level(
                    client, [entry.callback for entry in rollback_entries], "on_rollback()"
                )
        finally:
            TransactionEnding.record_transaction_end(client, event, cause)

    @staticmethod
    async def finish_after_failed_commit(client: TransactionClient, commit_error: BaseException) -> None:
        """Finishes a top-level transaction whose COMMIT was rejected: it is rolled back, so the
        ``on_rollback()`` callbacks run. ``commit_error`` stays the exception raised - a failing
        callback is logged and attached to it as a note.

        Args:
            client: The transaction's client.
            commit_error: The exception the COMMIT raised.
        """
        client._finalized = True
        try:
            await TransactionEnding.finish_top_level_operation(client, TransactionEventType.ROLLBACK)
        except Exception as callback_error:
            client.log.error("on_rollback() callback failed after a rejected COMMIT", exc_info=callback_error)
            commit_error.add_note(f"on_rollback() callback failed after the rejected COMMIT: {callback_error!r}")

    @staticmethod
    async def finish_after_lost_connection(
        client: TransactionClient, error: BaseException, *, commit_outcome_unknown: bool
    ) -> None:
        """Completes a top-level transaction whose connection was lost before its COMMIT/ROLLBACK
        could land: the server rolls such a transaction back, so it is finalized and reported as
        a ROLLBACK. A failing callback is logged and attached to ``error`` as a note.

        Args:
            client: The transaction's client.
            error: the connection error, which stays the exception that propagates.
            commit_outcome_unknown: True when the connection broke while a COMMIT was in flight -
                it may have landed, so no callback runs and the ROLLBACK event carries ``error``.
        """
        client._finalized = True
        cause = error if isinstance(error, Exception) else None
        try:
            await TransactionEnding.finish_top_level_operation(
                client, TransactionEventType.ROLLBACK, run_callbacks=not commit_outcome_unknown, cause=cause
            )
        except Exception as callback_error:
            client.log.error("on_rollback() callback failed after the connection was lost", exc_info=callback_error)
            error.add_note(f"on_rollback() callback failed after the connection was lost: {callback_error!r}")

    @staticmethod
    async def hold_transaction_for_ending(
        client: TransactionClient, *, give_up_on_timeout: bool
    ) -> tuple[SavepointSpan | None, contextvars.Token[SavepointSpan | None] | None]:
        """Marks this top-level transaction as ending, waits until no statement or savepoint of another
        task is open on it and holds it until ``TransactionEnding.stop_holding_transaction()`` - the COMMIT/ROLLBACK
        never lands while a sibling's statement is on the wire, and none starts after it.

        Args:
            client: The transaction's client.
            give_up_on_timeout: Raise when the wait times out - True for a COMMIT; a ROLLBACK goes
                ahead anyway.

        Returns:
            The span held and the token restoring the current span - both for
            ``TransactionEnding.stop_holding_transaction()``.

        Raises:
            TransactionManagementError: The wait timed out and ``give_up_on_timeout`` is set.
        """
        client._ending = True
        savepoint_lock = client._savepoint_lock
        parent_span = savepoint_lock.get_own_ancestor_span(current_savepoint_span.get())
        span: SavepointSpan | None
        try:
            span = await savepoint_lock.acquire(parent_span, timeout_seconds=TRANSACTION_END_WAIT_TIMEOUT_SECONDS)
        except TimeoutError:
            if give_up_on_timeout:
                raise TransactionManagementError(
                    "Nothing was committed: a concurrent asyncio.gather()/TaskGroup task still runs a "
                    "statement or holds a savepoint of this transaction after "
                    f"{TRANSACTION_END_WAIT_TIMEOUT_SECONDS:.0f}s - let every task working on a "
                    "transaction finish before the transaction ends."
                ) from None
            client.log.warning("Rolling back transaction %s while another task still uses it", client.connection_alias)
            span = None
        span_token = current_savepoint_span.set(span) if span is not None else None
        client._ending_span = span
        return span, span_token

    @staticmethod
    def stop_holding_transaction(
        client: TransactionClient,
        span: SavepointSpan | None,
        span_token: contextvars.Token[SavepointSpan | None] | None,
    ) -> None:
        """Ends what ``TransactionEnding.hold_transaction_for_ending()`` holds.

        Args:
            client: The transaction's client.
            span: The span it holds.
            span_token: The token restoring the current span.
        """
        client._ending_span = None
        if span_token is not None:
            current_savepoint_span.reset(span_token)
        if span is not None:
            client._savepoint_lock.release(span)

    @staticmethod
    async def run_shielded_from_cancellation(
        coroutine: Awaitable[Any],
        *,
        on_landed: Callable[[], None],
        on_timeout: Callable[[asyncio.Future[Any]], None] | None = None,
    ) -> Any:
        """Runs ``coroutine`` to completion even if the awaiting task is cancelled, calls ``on_landed``
        once it finished without error, and only then lets the cancellation through - a
        COMMIT/ROLLBACK can land on the server after its task was cancelled, and the bookkeeping
        must agree with the database.

        After a cancellation the wait is bounded (``SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS``): a
        dead server must not hang it. When the bound is hit the task is left running, ``on_timeout``
        gets it - to keep a resource the task may still use - and the cancellation goes through.

        A coroutine starts eagerly, up to its first suspension. Returns the coroutine's result.
        """
        if asyncio.iscoroutine(coroutine):
            task: asyncio.Future[Any] = asyncio.Task(coroutine, loop=asyncio.get_running_loop(), eager_start=True)
            if task.done():
                # Finished without suspending - nothing for a cancellation to interrupt.
                result = task.result()
                on_landed()
                return result
        else:
            task = asyncio.ensure_future(coroutine)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await TransactionEnding.wait_through_cancellations(task, on_timeout)
            raise
        finally:
            if task.done() and not task.cancelled() and task.exception() is None:
                on_landed()

    @staticmethod
    async def wait_through_cancellations(
        task: asyncio.Future[Any], on_timeout: Callable[[asyncio.Future[Any]], None] | None
    ) -> None:
        """Waits for a shielded ``task`` after the awaiting task was cancelled, ignoring any number
        of further cancellations, bounded by SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS.

        Args:
            task: the shielded operation.
            on_timeout: called with ``task`` when the bound is hit while it still runs.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS
        while not task.done():
            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                if on_timeout is not None:
                    on_timeout(task)
                return
            try:
                await asyncio.wait([task], timeout=remaining_seconds)
            except asyncio.CancelledError:
                continue

    @staticmethod
    def record_transaction_end(
        client: TransactionClient, event: TransactionEventType, cause: Exception | None = None
    ) -> None:
        """Reports a top-level COMMIT/ROLLBACK to the observers - never a savepoint's.

        Args:
            client: The transaction's client.
            event: The COMMIT or the ROLLBACK.
            cause: The error the transaction rolled back on, None for none.
        """
        started_at = client._transaction_started_at
        if started_at is None:
            return
        duration_ms = (time.perf_counter() - started_at) * 1000
        Observers.record_transaction(event, client.connection_alias, duration_ms, cause)
