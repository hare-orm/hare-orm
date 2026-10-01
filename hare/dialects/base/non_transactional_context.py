from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.transaction_context import TransactionContext


class NonTransactionalContext(TransactionContext):
    """The block ``DatabaseClient._in_transaction`` opens on a database without transactions: it yields
    the client itself - every statement is applied on its own and a failure rolls nothing back.
    """

    def __init__(self, client: DatabaseClient) -> None:
        self.statement_client = client

    async def __aenter__(self) -> TransactionClient:
        self._mark_entered()
        # The plain client stands in for a transaction's - callers tell them apart with isinstance().
        return self.statement_client  # type: ignore[return-value]

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        return None
