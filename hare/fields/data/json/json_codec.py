from __future__ import annotations

import datetime
import json
from typing import Any, ClassVar, cast
from uuid import UUID

from hare.fields.constants import JSON_LONG_INTEGER_MARKER, JSON_LONG_INTEGER_MIN_DIGITS
from hare.fields.data.constants import JSON_DIGIT_MARKER_TABLE
from hare.native.native_modules import NativeModules


class JsonCodec:
    """The default JSONField encoder/decoder pair - orjson (an optional accelerator) when it's
    installed, the standard library otherwise."""

    orjson: Any
    try:
        import orjson
    except ImportError:  # pragma: nocoverage
        orjson = None

    #: rust.native.rows.check_json_storable() - JSONField.check_storable_value()'s walk, done
    #: natively - None without the compiled accelerator or with a build that predates it.
    storable_value_checker: ClassVar[Any] = getattr(NativeModules.rows, "check_json_storable", None)
    #: rust.native.rows.encode_json() - the text orjson writes for a plain JSON value, checked on the
    #: same pass - None without the compiled accelerator or with a build that predates it.
    native_encoder: ClassVar[Any] = getattr(NativeModules.rows, "encode_json", None)

    @staticmethod
    def dumps(value: Any) -> str:
        """Encodes ``value`` as compact JSON text.

        Args:
            value: The value to encode.

        Returns:
            The JSON text.
        """
        if JsonCodec.orjson is None:  # pragma: nocoverage
            return json.dumps(
                value, separators=(",", ":"), ensure_ascii=False, default=JsonCodec.get_orjson_compatible_value
            )
        return cast("str", JsonCodec.orjson.dumps(value).decode())

    @staticmethod
    def dumps_exact(value: Any) -> str:
        """Encodes ``value`` with the standard library, which writes an int of any size exactly.

        Args:
            value: The value to encode.

        Returns:
            The JSON text.
        """
        return json.dumps(
            value, separators=(",", ":"), ensure_ascii=False, default=JsonCodec.get_orjson_compatible_value
        )

    @staticmethod
    def get_orjson_compatible_value(value: Any) -> Any:
        """The JSON-native form orjson writes for a value the standard library can't encode.

        Args:
            value: A value ``json.dumps()`` has no encoding for.

        Returns:
            ISO 8601 text for a datetime, date or naive time, the canonical text of a UUID.

        Raises:
            TypeError: orjson doesn't encode the value either (an aware time among them).
        """
        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()
        if isinstance(value, datetime.time):
            if value.tzinfo is not None:
                raise TypeError("datetime.time must not have tzinfo")
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        raise TypeError(f"Type is not JSON serializable: {type(value).__name__}")

    @staticmethod
    def has_long_integer(text: str | bytes) -> bool:
        """Whether ``text`` has a digit run long enough to be an integer orjson can't decode exactly.

        Args:
            text: JSON text.

        Returns:
            True for a run of at least ``JSON_LONG_INTEGER_MIN_DIGITS`` digits.
        """
        if len(text) < JSON_LONG_INTEGER_MIN_DIGITS:
            return False
        encoded_text = text.encode() if isinstance(text, str) else bytes(text)
        return JSON_LONG_INTEGER_MARKER in encoded_text.translate(JSON_DIGIT_MARKER_TABLE)

    @staticmethod
    def loads(text: str | bytes) -> Any:
        """Decodes JSON text, reading an integer of any size exactly.

        Args:
            text: The JSON text.

        Returns:
            The decoded value.
        """
        if JsonCodec.orjson is None or JsonCodec.has_long_integer(text):
            return json.loads(text)
        return JsonCodec.orjson.loads(text)
