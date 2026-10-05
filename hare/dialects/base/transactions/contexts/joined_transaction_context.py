from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext

if TYPE_CHECKING:
    from types import TracebackType

    from hare.dialects.base.client.transaction_client import TransactionClient


class JoinedTransactionContext(TransactionContext):
    """The block of a nested ``atomic()`` on a database whose transactions have no savepoints: it runs
    in the transaction it is nested in. Its statements can't be undone apart from the others, so an
    error leaving the block has the whole transaction roll back - its commit is refused.
    """

    __slots__ = ("client",)

    def __init__(self, client: TransactionClient) -> None:
        self.client = client

    async def __aenter__(self) -> TransactionClient:
        self._mark_entered()
        self.client._check_statement_allowed()
        return self.client

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        if exception is not None:
            self.client._mark_rollback_only(exception)
