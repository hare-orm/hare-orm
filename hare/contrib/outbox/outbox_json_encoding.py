from __future__ import annotations

import base64
import datetime
import decimal
import enum
import ipaddress
import json
import uuid
from collections.abc import Callable
from operator import attrgetter, methodcaller
from typing import Any, ClassVar


class OutboxJsonEncoding:
    """How an outbox event's payload and headers become JSON - an explicit table of the values
    besides JSON's own: a ``Decimal`` as its exact text, a ``UUID`` as text, a date, time or
    datetime as ISO 8601, a ``timedelta`` as its seconds, an IP address or network as text, ``bytes``
    as base64, an enum member as its value. Any other value raises
    ``TypeError`` - a ``ValidationError`` of the field when the event is written - never quietly
    becomes its ``str()``. The encoder of the
    ``payload`` and ``headers`` columns - the same for ``enqueue()`` and a captured change.
    """

    @staticmethod
    def get_base64_text(value: bytes | bytearray | memoryview) -> str:
        """Bytes as base64 text.

        Args:
            value: The bytes.

        Returns:
            The text.
        """
        return base64.b64encode(bytes(value)).decode("ascii")

    @staticmethod
    def get_json_value(value: Any) -> Any:
        """A value JSON doesn't write itself, as one it does.

        Args:
            value: The value.

        Returns:
            Its JSON form.

        Raises:
            TypeError: The value has no JSON form here.
        """
        for value_class in type(value).__mro__:
            converter = OutboxJsonEncoding.CONVERTERS.get(value_class)
            if converter is not None:
                return converter(value)
        raise TypeError(
            f"An outbox event can't hold a {type(value).__name__} ({value!r}) - give it a JSON value, a Decimal, "
            "UUID, date, time, datetime, bytes or enum member"
        )

    @staticmethod
    def dumps(value: Any) -> str:
        """Encodes a payload or headers as JSON text.

        Args:
            value: The payload or headers.

        Returns:
            The JSON text.

        Raises:
            TypeError: A value has no JSON form here.
        """
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=OutboxJsonEncoding.get_json_value)

    #: The JSON form of each class of value JSON doesn't write itself - a subclass's by its nearest
    #: class here.
    CONVERTERS: ClassVar[dict[type, Callable[[Any], Any]]] = {
        enum.Enum: attrgetter("value"),
        decimal.Decimal: str,
        uuid.UUID: str,
        datetime.date: methodcaller("isoformat"),
        datetime.time: methodcaller("isoformat"),
        datetime.timedelta: methodcaller("total_seconds"),
        **dict.fromkeys(
            (ipaddress.IPv4Address, ipaddress.IPv6Address, ipaddress.IPv4Network, ipaddress.IPv6Network), str
        ),
        **dict.fromkeys((bytes, bytearray, memoryview), get_base64_text),
    }
