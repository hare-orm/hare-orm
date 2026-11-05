from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """The outcome of one event, independent of its position in a batch.

    ``error=None`` acknowledges delivery. Metadata maps application fields of the outbox
    row to attempt results; only the store writes it, while the batch token still matches.
    Transport implementations never update the database themselves.
    """

    event_id: UUID
    error: BaseException | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
