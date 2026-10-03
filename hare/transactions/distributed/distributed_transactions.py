from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.client.transaction_client import TransactionClient

if TYPE_CHECKING:  # pragma: nocoverage
    pass


class DistributedTransactions:
    """Yielded by ``Transactions.distributed()`` - ``.coordinator`` and ``[alias]`` give the
    already-open ``TransactionClient`` for the coordinator/each participant, to pass as
    ``using=`` to any ORM call inside the block."""

    __slots__ = ("coordinator", "coordinator_alias", "_participants")

    def __init__(
        self,
        coordinator_alias: str,
        coordinator: TransactionClient,
        participants: dict[str, TransactionClient],
    ) -> None:
        self.coordinator_alias = coordinator_alias
        self.coordinator = coordinator
        self._participants = participants

    def __getitem__(self, alias: str) -> TransactionClient:
        if alias == self.coordinator_alias:
            return self.coordinator
        return self._participants[alias]
