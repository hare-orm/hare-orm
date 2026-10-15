from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext


@dataclass(frozen=True, slots=True)
class OpenDistributedTransaction:
    """A distributed transaction whose transactions are all open.

    Attributes:
        coordinator: The coordinator's connection alias.
        participants: The participants' aliases.
        xid: The transaction's id.
        coordinator_context: The coordinator's transaction context.
        coordinator_client: The coordinator's transaction.
        participant_contexts: The participants' transaction contexts by connection alias.
        participant_clients: The participants' transactions by connection alias.
    """

    coordinator: str
    participants: Sequence[str]
    xid: str
    coordinator_context: TransactionContext
    coordinator_client: TransactionClient
    participant_contexts: dict[str, TransactionContext]
    participant_clients: dict[str, TransactionClient]
