from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True, slots=True)
class OutboxBacklog:
    """The state of an outbox's queue - ``OutboxRelay.get_backlog()``, for a health check.

    Attributes:
        pending: The events still to deliver.
        oldest_pending_age_seconds: How long ago the oldest of them was written, 0 without any.
        dead_lettered: The events that ran out of attempts.
    """

    pending: int
    oldest_pending_age_seconds: float
    dead_lettered: int
