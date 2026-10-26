from __future__ import annotations

import base64
import datetime
import decimal
import enum
import ipaddress
import math
import uuid
from collections.abc import Callable, Mapping
from operator import attrgetter, methodcaller
from typing import Any, ClassVar

from hare.fields.constants import NAN_FLOAT_TEXT, NON_FINITE_FLOAT_TEXTS


class JsonValues:
    """The JSON form of a Python value - JSON values only: a ``Decimal`` as its exact text, a ``UUID`` as
    text, a date, time or datetime as ISO 8601, a ``timedelta`` as its seconds, an IP address or network as
    text, ``bytes`` as base64, an enum member as its value, a float JSON has no number for (``NaN``,
    ``Infinity``) as its text, a tuple as a list. Any other value raises ``TypeError`` - it never quietly
    becomes its ``str()``. The default of ``Field.get_json_value()``; a field whose values JSON has no
    form for here gives its own."""

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
        """A value's JSON form, every value inside a list, tuple or mapping too.

        Args:
            value: The value.

        Returns:
            Its JSON form.

        Raises:
            TypeError: The value, or one inside it, has no JSON form here - a mapping's key is no text.
        """
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            return NAN_FLOAT_TEXT if math.isnan(value) else NON_FINITE_FLOAT_TEXTS[value]
        if isinstance(value, (list, tuple)):
            return [JsonValues.get_json_value(item) for item in value]
        if isinstance(value, Mapping):
            return {JsonValues.get_json_key(key): JsonValues.get_json_value(item) for key, item in value.items()}
        return JsonValues.get_plain_json_value(value)

    @staticmethod
    def get_json_key(key: Any) -> str:
        """A mapping's key as a JSON object's key.

        Args:
            key: The key.

        Returns:
            The key - text, or the text form of an enum member's text value.

        Raises:
            TypeError: The key is no text.
        """
        if isinstance(key, enum.Enum):
            key = key.value
        if not isinstance(key, str):
            raise TypeError(f"A JSON object's key is text, not a {type(key).__name__} ({key!r})")
        return key

    @staticmethod
    def get_plain_json_value(value: Any) -> Any:
        """A single value JSON doesn't write itself, as one it does - by ``CONVERTERS``.

        Args:
            value: The value.

        Returns:
            Its JSON form.

        Raises:
            TypeError: The value has no JSON form here.
        """
        for value_class in type(value).__mro__:
            converter = JsonValues.CONVERTERS.get(value_class)
            if converter is not None:
                return converter(value)
        raise TypeError(
            f"A {type(value).__name__} ({value!r}) has no JSON form - give a JSON value, a Decimal, UUID, date, "
            "time, datetime, timedelta, IP address, bytes or enum member"
        )

    #: The JSON form of each class of value JSON doesn't write itself - a subclass's by its nearest class
    #: here.
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
