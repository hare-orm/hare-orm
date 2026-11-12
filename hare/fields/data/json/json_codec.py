from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from typing import Any, ClassVar
from uuid import UUID

import orjson

from hare.native.native_modules import NativeModules


class JsonCodec:
    """The default JSONField encoder/decoder pair - orjson, and the standard library's encoder for an
    integer past orjson's range."""

    #: rust.native.rows.check_json_storable() - JSONField.check_storable_value()'s walk, done
    #: natively.
    storable_value_checker: ClassVar[Any] = NativeModules.rows.check_json_storable
    #: rust.native.rows.loads_json() - decodes JSON text (``str`` or ``bytes``) with orjson, or with the
    #: native parser reading an integer of any size exactly when a run of 19 digits may be one past
    #: orjson's range. NaN and Infinity aren't JSON and are refused; invalid text raises ValueError.
    loads: ClassVar[Callable[[str | bytes], Any]] = NativeModules.rows.loads_json
    #: Shared ``rust.native.rows.dumps_json()`` encoding: ``orjson.dumps(value, default=default).decode()``.
    native_dumps: ClassVar[Callable[[Any, Callable[[Any], Any]], str]] = NativeModules.rows.dumps_json

    @staticmethod
    def dumps(value: Any) -> str:
        """Encodes ``value`` as compact JSON text.

        Args:
            value: The value to encode.

        Returns:
            The JSON text.
        """
        try:
            return JsonCodec.native_dumps(value, JsonCodec.get_orjson_compatible_value)
        except TypeError:
            # orjson takes no str subclass as a dict key (a StrEnum member) unless told to - a key of
            # another type stays refused.
            if not JsonCodec.has_only_text_keys(value):
                raise
        return orjson.dumps(
            value, default=JsonCodec.get_orjson_compatible_value, option=orjson.OPT_NON_STR_KEYS
        ).decode()

    @staticmethod
    def has_only_text_keys(value: Any) -> bool:
        """Whether every dict in ``value`` - at any depth - has text keys only.

        Args:
            value: The value.

        Returns:
            Whether it has.
        """
        pending = [value]
        # A container holding itself is walked once.
        seen: set[int] = set()
        while pending:
            item = pending.pop()
            if isinstance(item, (dict, list, tuple)):
                if id(item) in seen:
                    continue
                seen.add(id(item))
            if isinstance(item, dict):
                if not all(isinstance(key, str) for key in item):
                    return False
                pending.extend(item.values())
            elif isinstance(item, (list, tuple)):
                pending.extend(item)
        return True

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
            ISO 8601 text for a datetime, date or naive time, the canonical text of a UUID, the float of a
            float subclass (orjson writes those of int, str, dict and list only).

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
        if isinstance(value, float):
            return float(value)
        raise TypeError(f"Type is not JSON serializable: {type(value).__name__}")
