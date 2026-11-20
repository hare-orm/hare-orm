from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.transactions.savepoints.savepoint_span import SavepointSpan


@dataclass(eq=False, slots=True)
class RelationRollbackReset:
    """A fetched relation invalidation registered for one exact transaction scope."""

    client: TransactionClient
    span: SavepointSpan | None
