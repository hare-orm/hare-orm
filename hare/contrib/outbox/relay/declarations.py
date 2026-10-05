"""What an ``OutboxRelay`` reports through ``Observers`` - ``Observers.observe(OutboxDelivered, ...)``."""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.outbox.outbox_event import OutboxEvent


@dataclasses.dataclass(frozen=True, slots=True)
class OutboxDelivered:
    """An event a relay delivered.

    Attributes:
        model: The outbox model.
        event_id: The event's id.
        topic: Its topic.
        attempts: The failed attempts before this one.
        delay_seconds: How long after it was written it was delivered.
    """

    observed_by_model: ClassVar[bool] = False

    model: type[OutboxEvent]
    event_id: uuid.UUID
    topic: str
    attempts: int
    delay_seconds: float


@dataclasses.dataclass(frozen=True, slots=True)
class OutboxDeliveryFailed:
    """An event a relay failed to deliver and will try again.

    Attributes:
        model: The outbox model.
        event_id: The event's id.
        topic: Its topic.
        attempts: The failed attempts, this one included.
        error: What failed it.
        next_attempt_at: When it is tried again.
    """

    observed_by_model: ClassVar[bool] = False

    model: type[OutboxEvent]
    event_id: uuid.UUID
    topic: str
    attempts: int
    error: str
    next_attempt_at: datetime.datetime


@dataclasses.dataclass(frozen=True, slots=True)
class OutboxDeadLettered:
    """An event that ran out of attempts - no longer delivered until ``retry_dead_lettered()``.

    Attributes:
        model: The outbox model.
        event_id: The event's id.
        topic: Its topic.
        attempts: The failed attempts.
        error: What failed the last one.
    """

    observed_by_model: ClassVar[bool] = False

    model: type[OutboxEvent]
    event_id: uuid.UUID
    topic: str
    attempts: int
    error: str
