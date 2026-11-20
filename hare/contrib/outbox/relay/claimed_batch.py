from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar
from uuid import UUID

from hare.contrib.outbox.outbox_event import OutboxEvent

TOutboxEvent = TypeVar("TOutboxEvent", bound=OutboxEvent)


@dataclass(frozen=True, slots=True)
class ClaimedBatch(Generic[TOutboxEvent]):
    """Events leased by one claim; the token changes even when the relay name stays the same."""

    token: UUID
    events: tuple[TOutboxEvent, ...]
