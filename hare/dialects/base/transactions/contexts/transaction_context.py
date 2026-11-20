from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from hare.exceptions import (
    TransactionManagementError,
)

if TYPE_CHECKING:
    from types import TracebackType

    from hare.dialects.base.client.transaction_client import TransactionClient


class TransactionContext:
    """A context manager interface for transactions. It is returned from atomic()
    and _in_transaction."""

    client: TransactionClient
    _entered: bool = False

    def _mark_entered(self) -> None:
        """Marks this context as used - a transaction context is single-use.

        Raises:
            TransactionManagementError: if this context was already entered once.
        """
        if self._entered:
            raise TransactionManagementError(
                "This transaction context was already used - create a new one with atomic()"
            )
        self._entered = True

    @abc.abstractmethod
    async def __aenter__(self) -> TransactionClient: ...

    @abc.abstractmethod
    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None: ...

    @staticmethod
    async def _roll_back_keeping_error(
        client: TransactionClient, error: BaseException | None, *, after_failed_release: bool = False
    ) -> None:
        """Attach rollback failures to the active error. Cancellation of the caller propagates
        after rollback cleanup, even when the block already raised.

        Args:
            client: The transaction or savepoint being rolled back.
            error: the exception propagating out of the block, None when there is none.
            after_failed_release: Whether the savepoint release already failed.
        Raises:
            BaseException: Caller cancellation, or the rollback failure when ``error`` is None.
        """
        wait_cancelled = False

        def mark_wait_cancelled() -> None:
            nonlocal wait_cancelled
            wait_cancelled = True

        is_savepoint = client._savepoint_name is not None
        rollback = client.savepoint_rollback if is_savepoint else client.rollback
        try:
            await rollback(_on_wait_cancelled=mark_wait_cancelled)
        except BaseException as rollback_error:
            if wait_cancelled or error is None:
                raise
            if client.transaction_state.finished:
                client.log.error("on_rollback() callback failed", exc_info=rollback_error)
                completed = "rolling back the savepoint" if is_savepoint else "the rollback"
                error.add_note(f"on_rollback() callback failed after {completed}: {rollback_error!r}")
            else:
                action = "Rolling back the savepoint" if is_savepoint else "Rolling back the transaction"
                if after_failed_release:
                    action += " after its failed release"
                client.log.error("%s failed", action, exc_info=rollback_error)
                error.add_note(f"{action} also failed: {rollback_error!r}")
