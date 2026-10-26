from __future__ import annotations

import json
from typing import Any

from hare.fields.json_values import JsonValues


class OutboxJsonEncoding:
    """How an outbox event's payload and headers become JSON - the values ``JsonValues`` writes: a
    ``Decimal`` as its exact text, a ``UUID`` as text, a date, time or datetime as ISO 8601, a ``timedelta``
    as its seconds, an IP address or network as text, ``bytes`` as base64, an enum member as its value. Any
    other value raises ``TypeError`` and a float JSON has no number for ``ValueError`` - a ``ValidationError``
    of the field when the event is written - never invalid JSON or a value's ``str()``. A captured change
    holds each value in its field's JSON form already (``Field.get_json_value()``). The encoder of the
    ``payload`` and ``headers`` columns - the same for ``enqueue()`` and a captured change.
    """

    @staticmethod
    def dumps(value: Any) -> str:
        """Encodes a payload or headers as JSON text.

        Args:
            value: The payload or headers.

        Returns:
            The JSON text.

        Raises:
            TypeError: A value has no JSON form here.
            ValueError: A float is NaN or infinite - JSON has no number for it.
        """
        return json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=JsonValues.get_plain_json_value,
        )
