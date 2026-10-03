from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.transactions.enums import DistributedTransactionResolution

if TYPE_CHECKING:  # pragma: nocoverage
    pass


@dataclass
class StalePreparedTransaction:
    """A prepared transaction recovery found unresolved: one to ``COMMIT PREPARED`` (the coordinator's
    decision landed) or to ``ROLLBACK PREPARED`` (no decision was recorded).

    Args:
        xid: The GID.
        participant_alias: The alias of the database holding it.
        resolution: What should happen to it.
        reason: Why - shown by ``hare distributed-recover``.
    """

    xid: str
    participant_alias: str
    resolution: DistributedTransactionResolution
    reason: str
