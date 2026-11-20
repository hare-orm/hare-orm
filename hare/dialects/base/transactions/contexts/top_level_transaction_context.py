from __future__ import annotations

import time
import weakref
from typing import TYPE_CHECKING

from hare.core.connections.connections import Connections
from hare.instrumentation.observers.observers import Observers
from hare.transactions.enums import TransactionEventType

if TYPE_CHECKING:
    from types import TracebackType

    from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext


class TopLevelTransactionContext(TransactionContext):
    """The context of a top-level transaction on any driver: takes what the transaction holds for its
    whole life, begins, and on exit commits or rolls back and gives the resources back as soon as
    the COMMIT/ROLLBACK landed.
    """

    __slots__ = ("client", "connection_alias", "token", "_resources_released")

    def __init__(self, client: TransactionClient) -> None:
        self.client = client
        self.connection_alias = client.connection_alias
        self._resources_released = False
        client._transaction_context_reference = weakref.ref(self)

    async def __aenter__(self) -> TransactionClient:
        self._mark_entered()
        connections = self.client.connection_handler
        if connections is None:
            connections = Connections.current()
            self.client.connection_handler = connections
        await self.client._take_transaction_resources()
        # The first checkout can open the parent and discover version-dependent features.
        self.client.features = self.client._parent.features
        # Set before BEGIN, so the current task sees the transaction's client from the start.
        application = connections.application
        self.token = connections.set(self.connection_alias, self.client)
        try:
            await self.client.begin()
        except BaseException:
            # __aexit__ never runs when __aenter__ raises - nothing may stay held or pointed at.
            self.token.reset()
            try:
                await self.client._undo_failed_begin()
            finally:
                await self._release_resources()
            raise
        if connections.application is not application:
            # A reconnect while BEGIN waited acquired a connection of the new lifetime.
            # Its old frame was invalidated; publish the successfully begun client again.
            self.token.reset()
            self.token = connections.set(self.connection_alias, self.client)
        self._record_transaction_begin(self.client)
        await self._apply_transaction_restrictions(self.client)
        return self.client

    async def _release_resources(self) -> None:
        """Gives the transaction's resources back, once - the client calls this as soon as its
        real COMMIT/ROLLBACK has landed, and __aexit__ calls it again for every path that did not
        get that far."""
        if self._resources_released:
            return
        self._resources_released = True
        await self.client._give_back_transaction_resources()

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        in_flight: BaseException | None = exception
        try:
            await self._commit_or_rollback(self.client, exception_type, exception)
        except BaseException as error:
            in_flight = error
            raise
        finally:
            try:
                try:
                    await self.client._end_unfinished_transaction()
                finally:
                    await self._release_resources()
            except Exception as release_error:
                self.client._release_failure = release_error
            try:
                self._report_release_failure(self.client, in_flight)
            finally:
                self.token.reset()

    @staticmethod
    def _record_transaction_begin(client: TransactionClient) -> None:
        """Reports the BEGIN to the observers and stamps ``client._transaction_started_at`` - for a
        top-level transaction only, never a savepoint.
        """
        client._transaction_started_at = time.perf_counter()
        Observers.record_transaction(TransactionEventType.BEGIN, client.connection_alias, 0.0, None)

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
        client: TransactionClient, exception_type: type[BaseException] | None, exception: BaseException | None = None
    ) -> None:
        """Commits, or rolls back when the block raised - the exit of every top-level transaction
        context. ``client.commit()`` runs the ``on_commit()`` callbacks itself.

        Args:
            client: The transaction's client.
            exception_type: The type of the exception the block raised, if any.
            exception: The exception the block raised, if any. A failure while rolling back never
                replaces it - it is logged and attached as a note.
        """
        if client.transaction_state.finished:
            return
        if not exception_type:
            try:
                await client.commit()
            except Exception as commit_error:
                # A COMMIT that failed before reaching the server leaves the transaction open.
                if not client.transaction_state.finished and not client._shielded_operation_abandoned:
                    await TopLevelTransactionContext._roll_back_keeping_error(client, commit_error)
                raise
            return
        await TopLevelTransactionContext._roll_back_keeping_error(client, exception)

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
