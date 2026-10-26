from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any


@dataclasses.dataclass(frozen=True, slots=True)
class OutboxMessage:
    """One event of ``OutboxEvent.enqueue_many()`` - the arguments ``enqueue()`` takes for one event.

    Attributes:
        topic: The event's topic - a delivery routes on it.
        payload: The event's payload - JSON values, or values ``OutboxJsonEncoding`` writes.
        idempotency_key: At most one event ever exists per key - the one stored is returned instead.
        ordering_key: Events of one key are delivered strictly in the order they were written.
        headers: Metadata of the event besides its payload.
        extra_field_values: Values of the columns a subclass declares on top of ``OutboxEvent``'s own.
    """

    topic: str
    payload: Any
    idempotency_key: str | None = None
    ordering_key: str | None = None
    headers: Mapping[str, Any] | None = None
    extra_field_values: Mapping[str, Any] | None = None
