import abc
import time
import weakref
from typing import TYPE_CHECKING, Any

from hare.exceptions import (
    TransactionManagementError,
)
from hare.instrumentation.observers import Observers
from hare.transactions.enums import TransactionEventType

if TYPE_CHECKING:
    from hare.dialects.base.client.transaction_client import TransactionClient


class TransactionContext:
    """A context manager interface for transactions. It is returned from atomic()
    and _in_transaction."""

    client: TransactionClient
    _entered: bool = False

    def _link_client(self, client: TransactionClient) -> None:
        """Lets ``client`` release this context's resources as soon as its real COMMIT/ROLLBACK
        has landed."""
        client._transaction_context_ref = weakref.ref(self)

    async def _release_resources(self) -> None:
        """Hands back whatever this context acquired for its transaction - nothing by default.
        Idempotent."""

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
    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None: ...

    @staticmethod
    def _record_transaction_begin(client: TransactionClient) -> None:
        """Reports the BEGIN to the observers and stamps ``client._transaction_started_at`` - for a
        top-level transaction only, never a savepoint.
        """
        client._transaction_started_at = time.monotonic()
        Observers.record_transaction(TransactionEventType.BEGIN, client.connection_name, 0.0, None)

    async def _apply_transaction_restrictions(self, client: TransactionClient) -> None:
        """Applies ``client``'s read-only mode/statement timeout right after a top-level BEGIN,
        ending the transaction through this context's own exit path if that fails.

        Args:
            client: the wrapper this context has just begun a transaction on.
        """
        try:
            await client._apply_transaction_restrictions()
        except BaseException as error:
            await self.__aexit__(type(error), error, error.__traceback__)
            raise

    @staticmethod
    async def _commit_or_rollback(
        client: TransactionClient, exc_type: type[BaseException] | None, exc_val: BaseException | None = None
    ) -> None:
        """Commits, or rolls back when the block raised - the exit of every top-level transaction
        context. ``client.commit()`` runs the ``on_commit()`` callbacks itself.

        Args:
            client: The transaction's client.
            exc_type: The type of the exception the block raised, if any.
            exc_val: The exception the block raised, if any. A failure while rolling back never
                replaces it - it is logged and attached as a note.
        """
        if client._finalized:
            return
        if not exc_type:
            try:
                await client.commit()
            except Exception as commit_error:
                # A COMMIT that failed before reaching the server leaves the transaction open.
                if not client._finalized and not client._shielded_operation_abandoned:
                    await TransactionContext._roll_back_keeping_error(client, commit_error)
                raise
            return
        await TransactionContext._roll_back_keeping_error(client, exc_val)

    @staticmethod
    async def _roll_back_keeping_error(client: TransactionClient, error: BaseException | None) -> None:
        """Rolls ``client``'s transaction back while ``error`` is propagating - a failure doing so
        is logged and attached to ``error`` as a note instead of replacing it.

        Args:
            client: the top-level transaction wrapper.
            error: the exception propagating out of the block, None when there is none.
        Raises:
            Exception: the rollback failure, when ``error`` is None.
        """
        try:
            await client.rollback()
        except Exception as rollback_error:
            if error is None:
                raise
            if client._finalized:
                # The ROLLBACK itself landed - only an on_rollback() callback failed.
                client.log.error("on_rollback() callback failed", exc_info=rollback_error)
                error.add_note(f"on_rollback() callback failed after the rollback: {rollback_error!r}")
            else:
                client.log.error("Rolling back the transaction failed", exc_info=rollback_error)
                error.add_note(f"Rolling back the transaction also failed: {rollback_error!r}")

    @staticmethod
    def _report_release_failure(client: TransactionClient, in_flight: BaseException | None) -> None:
        """Surfaces a failed release of the transaction's connection/lock without letting it
        replace the exception already propagating out of the block.

        Args:
            client: The transaction's client.
            in_flight: the exception already propagating out of this context, if any.

        Raises:
            Exception: the release failure, when nothing else is propagating.
        """
        release_failure = client._release_failure
        if release_failure is None:
            return
        if in_flight is None:
            raise release_failure
        client.log.error("Releasing the transaction's connection failed", exc_info=release_failure)
        in_flight.add_note(f"Releasing the transaction's connection also failed: {release_failure!r}")
