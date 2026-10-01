from __future__ import annotations

import dataclasses
from typing import ClassVar

from hare.transactions.enums import TransactionEventType


@dataclasses.dataclass(frozen=True, slots=True)
class TransactionEvent:
    """A real, top-level transaction began, committed or rolled back - never a savepoint.

    Attributes:
        type: Begin, commit or rollback.
        connection_name: The connection the transaction runs on.
        duration_ms: 0.0 for a begin; the time since the begin for a commit or rollback.
        error: The error a rollback was caused by - a lost connection carries the connection
            error (when it broke while the COMMIT was in flight, the COMMIT may still have landed,
            and no ``on_commit()``/``on_rollback()`` callback runs); None otherwise.
    """

    #: Observers of this event can't be narrowed to models.
    observed_by_model: ClassVar[bool] = False

    type: TransactionEventType
    connection_name: str
    duration_ms: float
    error: Exception | None
