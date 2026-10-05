from __future__ import annotations

import asyncio
import contextvars
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from types import TracebackType

    from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext
from hare.dialects.base.transactions.savepoints.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.base.transactions.savepoints.savepoint_span import current_savepoint_span


class NestedTransactionContext(TransactionContext):
    __slots__ = ("client", "connection_alias", "_savepoint_lock", "_span", "_span_reset_token")

    #: The background releases of spans still running - the event loop keeps a task only weakly, so
    #: an unreferenced one could be collected before it releases its span.
    span_releases: ClassVar[set[asyncio.Future[None]]] = set()

    def __init__(self, client: TransactionClient, savepoint_lock: NestedSavepointLock) -> None:
        self.client = client
        self.connection_alias = client.connection_alias
        # Shared by every nested client of one top-level transaction; held for this context's whole
        # life.
        self._savepoint_lock = savepoint_lock

    async def __aenter__(self) -> TransactionClient:
        self._mark_entered()
        # Sibling nested transactions share one connection, and savepoints close last-in-first-out:
        # one sibling's whole block runs before another's starts. The span is taken under the one
        # this context was spawned in; a span of another connection's lock is filtered out.
        parent_span = self._savepoint_lock.get_own_ancestor_span(current_savepoint_span.get())
        self._span = await self._savepoint_lock.acquire(parent_span)
        # The span identifies this savepoint's callbacks (see TransactionCallback) - kept on the
        # client too, so a manual `await inner.rollback()` discards the right ones.
        self.client._savepoint_span = self._span
        self._span_reset_token = current_savepoint_span.set(self._span)
        try:
            # The transaction may have started ending (or ended) while this waited for its turn.
            self.client._check_statement_allowed()
            await self.client.savepoint()
        except BaseException:
            # __aexit__ never runs if __aenter__ itself raises - without this, a failed
            # savepoint() would permanently hold the span open, wedging every future sibling
            # nested transaction spawned under the same parent on this connection.
            try:
                self._savepoint_lock.release(self._span)
            finally:
                self._reset_span_context()
            raise
        return self.client

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        try:
            if not self.client._finalized and self.client._get_top_level_transaction()._finalized:
                # The whole transaction already ended (a manual commit()/rollback() of an outer
                # level inside this block) - the savepoint ended with it, nothing is left to send.
                self.client._finalized = True
            elif not self.client._finalized:
                if exception is not None:
                    await self._rollback_savepoint_keeping_error(exception, "Rolling back the savepoint")
                else:
                    # Not a real commit - a savepoint inside the outer transaction, which can
                    # itself still roll back. The callbacks only actually run once the outermost
                    # level reaches a real commit().
                    try:
                        await self.client.release_savepoint()
                        if self._span.statement_interrupted:
                            self.client._mark_interrupted_level(self._span.parent)
                    except Exception as release_error:
                        # A failed RELEASE (e.g. Postgres refusing it in an aborted transaction)
                        # leaves the savepoint open - rolling back to it keeps the outer
                        # transaction usable and discards this block's on_commit() callbacks.
                        if not self.client._finalized and self.client._shielded_savepoint_abandoned_task is None:
                            await self._rollback_savepoint_keeping_error(
                                release_error, "Rolling back the savepoint after its failed release"
                            )
                        raise
        finally:
            # A RELEASE/ROLLBACK TO that hit the shielded wait's bound may still be running - the
            # span is then released by a background task once it lands, so no sibling runs on the
            # connection meanwhile.
            #
            # The span is released before the ambient span is reset: the reset can fail when the
            # context is exited from another task, and must not leave the span open.
            try:
                abandoned_task = self.client._shielded_savepoint_abandoned_task
                if abandoned_task is not None:
                    self.client._shielded_savepoint_abandoned_task = None
                    span_release = asyncio.ensure_future(self._release_span_once_abandoned_task_lands(abandoned_task))
                    NestedTransactionContext.span_releases.add(span_release)
                    span_release.add_done_callback(NestedTransactionContext.span_releases.discard)
                else:
                    self._savepoint_lock.release(self._span)
            finally:
                self._reset_span_context()

    def _reset_span_context(self) -> None:
        """Restores the ambient savepoint span to what it was before __aenter__ - also when exited
        from a task other than the one that entered this context, whose own context the token does
        not belong to."""
        try:
            current_savepoint_span.reset(self._span_reset_token)
        except ValueError:
            if current_savepoint_span.get() is self._span:
                previous_span = self._span_reset_token.old_value
                current_savepoint_span.set(None if previous_span is contextvars.Token.MISSING else previous_span)

    async def _rollback_savepoint_keeping_error(self, error: BaseException, action: str) -> None:
        """Rolls back to this context's savepoint while ``error`` is propagating - a failure
        doing so (a failing on_rollback() callback, a lost connection) is logged and attached to
        ``error`` as a note instead of replacing it.

        Args:
            error: The exception propagating out of the block.
            action: What is being attempted, for the log message and the note.
        """
        try:
            await self.client.savepoint_rollback()
        except Exception as rollback_error:
            if self.client._finalized:
                # The ROLLBACK TO itself landed - only an on_rollback() callback failed.
                self.client.log.error("on_rollback() callback failed", exc_info=rollback_error)
                error.add_note(f"on_rollback() callback failed after rolling back the savepoint: {rollback_error!r}")
            else:
                self.client.log.error("%s failed", action, exc_info=rollback_error)
                error.add_note(f"{action} also failed: {rollback_error!r}")

    async def _release_span_once_abandoned_task_lands(self, task: asyncio.Future[Any]) -> None:
        """Releases this context's span once the still-running RELEASE/ROLLBACK TO has finished. Runs
        as a background task; its exceptions are swallowed.
        """
        try:
            await task
        except BaseException:
            pass
        finally:
            self._savepoint_lock.release(self._span)
