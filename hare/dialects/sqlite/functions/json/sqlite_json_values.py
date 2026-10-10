from __future__ import annotations

import datetime
import math
from decimal import Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_JSON_BYTES_FUNCTION_NAME,
    SQLITE_JSON_FLOAT_FUNCTION_NAME,
    SQLITE_JSON_TIME_FUNCTION_NAME,
    SQLITE_JSON_TIMESTAMP_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions


class SqliteJsonValues:
    """Writes values into a JSON object built on SQLite as Postgres's jsonb holds them."""

    @staticmethod
    def get_fraction_text(microsecond: int) -> str:
        """The fraction of a second as Postgres writes it - without trailing zeros, none for zero."""
        return f".{microsecond:06d}".rstrip("0") if microsecond else ""

    @staticmethod
    def format_float(value: float | None) -> str | None:
        """A double as JSON text: a number in positional notation (an integral one without a
        fraction), ``NaN``/``Infinity`` as strings.

        Args:
            value: The number.

        Returns:
            The JSON text, or ``None`` for NULL.
        """
        if value is None:
            return None
        if math.isnan(value):
            return '"NaN"'
        if math.isinf(value):
            return '"Infinity"' if value > 0 else '"-Infinity"'
        if value == 0:
            return "0"
        text = format(Decimal(repr(float(value))), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    @classmethod
    def format_timestamp(cls, value: str | None, is_aware: int) -> str | None:
        """A stored timestamp as Postgres writes it in JSON - an aware one as ISO text in UTC, a
        naive one as its wall clock.

        Args:
            value: The stored text - a naive one is in UTC when ``is_aware``.
            is_aware: Whether the value is written with its offset.

        Returns:
            The text, e.g. ``2020-01-02T03:04:05.5+00:00``; an unparsable value as it is.
        """
        if value is None:
            return None
        try:
            moment = datetime.datetime.fromisoformat(value)
        except ValueError:
            return value
        if not is_aware:
            if moment.tzinfo is not None:
                moment = moment.astimezone().replace(tzinfo=None)
            return f"{moment:%Y-%m-%dT%H:%M:%S}{cls.get_fraction_text(moment.microsecond)}"
        if moment.tzinfo is not None:
            moment = moment.astimezone(datetime.UTC)
        return f"{moment:%Y-%m-%dT%H:%M:%S}{cls.get_fraction_text(moment.microsecond)}+00:00"

    @classmethod
    def format_time(cls, value: str | None) -> str | None:
        """A stored time as Postgres writes a ``timetz`` in JSON - the offset's minutes only when
        not zero.

        Args:
            value: The stored text, a naive one in UTC.

        Returns:
            The text, e.g. ``12:30:00.25+00``, ``08:00:00+05:30``; an unparsable value as it is.
        """
        if value is None:
            return None
        try:
            clock = datetime.time.fromisoformat(value)
        except ValueError:
            return value
        offset_seconds = int(clock.utcoffset().total_seconds()) if clock.utcoffset() is not None else 0  # type: ignore[union-attr]
        sign = "-" if offset_seconds < 0 else "+"
        offset_hours, offset_minutes = divmod(abs(offset_seconds) // 60, 60)
        offset = f"{sign}{offset_hours:02d}" + (f":{offset_minutes:02d}" if offset_minutes else "")
        return f"{clock:%H:%M:%S}{cls.get_fraction_text(clock.microsecond)}{offset}"

    @staticmethod
    def format_bytes(value: bytes | None) -> str | None:
        """Bytes as Postgres writes a ``bytea`` in JSON - ``\\x`` and lower-case hex."""
        return None if value is None else "\\x" + bytes(value).hex()

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers the JSON value UDFs on a SQLite connection."""
        native_functions = SqliteNativeFunctions.module
        functions: Any = cls if native_functions is None else native_functions.JsonValues(cls)
        await connection.create_function(
            SQLITE_JSON_FLOAT_FUNCTION_NAME, 1, functions.format_float, deterministic=True
        )
        await connection.create_function(
            SQLITE_JSON_TIMESTAMP_FUNCTION_NAME, 2, functions.format_timestamp, deterministic=True
        )
        await connection.create_function(SQLITE_JSON_TIME_FUNCTION_NAME, 1, functions.format_time, deterministic=True)
        await connection.create_function(SQLITE_JSON_BYTES_FUNCTION_NAME, 1, cls.format_bytes, deterministic=True)
