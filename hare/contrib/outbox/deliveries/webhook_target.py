from __future__ import annotations

import dataclasses
from collections.abc import Mapping


@dataclasses.dataclass(frozen=True, slots=True)
class WebhookTarget:
    """Where an event's webhook goes - what ``WebhookDelivery(target=...)`` gives per event.

    Attributes:
        url: The endpoint.
        secret: The key the body is signed with; None sends it unsigned.
        headers: Headers of this target's requests.
    """

    url: str
    secret: str | bytes | None = None
    headers: Mapping[str, str] = dataclasses.field(default_factory=dict)
