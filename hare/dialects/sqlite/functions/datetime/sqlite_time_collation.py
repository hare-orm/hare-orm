from __future__ import annotations

import datetime
from typing import Any, ClassVar

import aiosqlite

from hare.core.caching.cache import Cache
from hare.dialects.sqlite.constants import SQLITE_TIME_COLLATION_NAME, SQLITE_TIME_SORT_KEY_FUNCTION_NAME
from hare.dialects.sqlite.functions.constants import (
    SQLITE_SORT_KEY_TEXT_CLASS,
    SQLITE_TIME_COLLATION_SORT_KEY_CACHE_SIZE,
)
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.functions.sqlite_sort_keys import SqliteSortKeys
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.sql.constants import MICROSECONDS_PER_SECOND


class SqliteTimeCollation:
    """Backs ``SQLITE_TIME_COLLATION_NAME`` - orders stored ``TimeField`` text the way Postgres
    orders ``TIMETZ``: by the UTC time (the wall clock minus its offset, not wrapped around
    midnight), then by the offset - two values are equal only with the same wall clock and offset.
    A naive time counts as UTC, as it is bound on Postgres. Text that isn't a time sorts after
    every time, by its own text."""

    #: (text,) -> its sort key.
    SORT_KEYS: ClassVar[Cache[tuple[int, int, int, str]]] = Cache(SQLITE_TIME_COLLATION_SORT_KEY_CACHE_SIZE)

    @classmethod
    def get_sort_key(cls, text: str) -> tuple[int, int, int, str]:
        """Sort key of one compared text.

        Args:
            text: The stored or bound text.

        Returns:
            ``(0, UTC microseconds, -offset seconds, "")`` for a time, ``(1, 0, 0, text)`` otherwise.
        """
        sort_key = cls.SORT_KEYS.get((text,))
        if sort_key is None:
            sort_key = cls.SORT_KEYS[(text,)] = cls.make_sort_key(text)
        return sort_key

    @staticmethod
    def make_sort_key(text: str) -> tuple[int, int, int, str]:
        """The sort key ``get_sort_key()`` keeps for a text."""
        try:
            value = datetime.time.fromisoformat(text)
        except ValueError:
            return 1, 0, 0, text
        offset = value.utcoffset() or datetime.timedelta(0)
        wall_clock_microseconds = (
            value.hour * 3600 + value.minute * 60 + value.second
        ) * MICROSECONDS_PER_SECOND + value.microsecond
        offset_microseconds = TimeDeltaField.get_microseconds(offset)
        return 0, wall_clock_microseconds - offset_microseconds, -offset_microseconds, ""

    @classmethod
    def get_sort_key_bytes(cls, value: int | float | str | bytes | None) -> bytes | None:
        """Backs ``SQLITE_TIME_SORT_KEY_FUNCTION_NAME``: the byte key ordering a value as the
        collation does.

        Args:
            value: Any SQLite value.

        Returns:
            The key; None for NULL.
        """
        if value is None:
            return None
        key = SqliteSortKeys.get_non_text_key(value)
        if key is not None:
            return key
        rank, utc_microseconds, negative_offset_microseconds, text = cls.get_sort_key(str(value))
        if rank == 0:
            return (
                SQLITE_SORT_KEY_TEXT_CLASS
                + b"\x01"
                + SqliteSortKeys.get_signed_key(utc_microseconds)
                + SqliteSortKeys.get_signed_key(negative_offset_microseconds)
            )
        return SQLITE_SORT_KEY_TEXT_CLASS + b"\x02" + text.encode()

    @classmethod
    def compare(cls, left: str, right: str) -> int:
        """Compares two time texts as Postgres compares ``TIMETZ`` values.

        Args:
            left: The left operand's text.
            right: The right operand's text.

        Returns:
            A negative number, zero or a positive number, like ``cmp()``.
        """
        left_key = cls.get_sort_key(left)
        right_key = cls.get_sort_key(right)
        return (left_key > right_key) - (left_key < right_key)

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers the collation on ``connection``.

        Args:
            connection: The SQLite connection to register the collation on.
        """
        native_functions = SqliteNativeFunctions.module
        compare: Any = cls.compare
        sort_key: Any = cls.get_sort_key_bytes
        if native_functions is not None:
            order = native_functions.TimeOrder(cls.get_sort_key_bytes, cls.compare)
            compare, sort_key = order.compare, order.sort_key
        await connection._execute(  # type: ignore[no-untyped-call]
            connection._conn.create_collation, SQLITE_TIME_COLLATION_NAME, compare
        )
        await connection.create_function(SQLITE_TIME_SORT_KEY_FUNCTION_NAME, 1, sort_key, deterministic=True)
