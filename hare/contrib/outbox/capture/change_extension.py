from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any


@dataclasses.dataclass(frozen=True, slots=True)
class ChangeExtension:
    """What ``ChangeCapture(extend=...)`` adds to the outbox event of a change - what a service knows
    of it and the ORM doesn't: its recipients, its scope, the author of the change.

    Attributes:
        payload: Keys added to the event's envelope - none of the envelope's own.
        headers: The event's headers.
        extra_field_values: Values of the columns the outbox model declares on top of
            ``OutboxEvent``'s own - its tenant among them.
    """

    payload: Mapping[str, Any] | None = None
    headers: Mapping[str, Any] | None = None
    extra_field_values: Mapping[str, Any] | None = None
