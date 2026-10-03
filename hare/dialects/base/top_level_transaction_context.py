from typing import TYPE_CHECKING, Any

from hare.core.connections import Connections

if TYPE_CHECKING:
    from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.transaction_context import TransactionContext


class TopLevelTransactionContext(TransactionContext):
    """The context of a top-level transaction on any driver: takes what the transaction holds for its
    whole life, begins, and on exit commits or rolls back and gives the resources back as soon as
    the COMMIT/ROLLBACK landed.
    """

    __slots__ = ("client", "connection_name", "token", "_resources_released")

    def __init__(self, client: TransactionClient) -> None:
        self.client = client
        self.connection_name = client.connection_name
        self._resources_released = False
        self._link_client(client)

    async def __aenter__(self) -> TransactionClient:
        self._mark_entered()
        await self.client._take_transaction_resources()
        # Set before BEGIN, so the current task sees the transaction's client from the start.
        self.token = Connections.current().set(self.connection_name, self.client)
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

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        in_flight: BaseException | None = exc_val
        try:
            await self._commit_or_rollback(self.client, exc_type, exc_val)
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
