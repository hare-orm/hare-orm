from __future__ import annotations

import datetime
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_DATE_DIFFERENCE_FUNCTION_NAME,
    SQLITE_DATE_SHIFT_FUNCTION_NAME,
    SQLITE_DATETIME_DIFFERENCE_FUNCTION_NAME,
    SQLITE_DATETIME_SHIFT_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.sql.constants import MICROSECONDS_PER_DAY
from hare.time import Timezone


class TemporalArithmeticFunctions:
    """Python UDFs backing date/datetime shift and difference arithmetic on SQLite (see
    ``TemporalShift``/``TemporalDifference``) - text in, text or whole microseconds out, in the
    stored ISO format."""

    @staticmethod
    def get_aware_datetime(value: str) -> datetime.datetime:
        """Parses a stored datetime text; a naive one (`use_timezone=False`) is read as local wall-clock time.

        Args:
            value: ISO datetime text as written by the SQLite backend's datetime adapter.

        Returns:
            A timezone-aware datetime, so arithmetic between values is on absolute time.
        """
        parsed = datetime.datetime.fromisoformat(value)
        return Timezone.make_system_local_aware(parsed) if parsed.tzinfo is None else parsed

    @classmethod
    def shift_datetime(cls, value: str | None, microseconds: int | None, sign: int) -> str | None:
        """Backs ``SQLITE_DATETIME_SHIFT_FUNCTION_NAME`` - the datetime moved by
        ``sign * microseconds`` of absolute time, in the text format the datetime adapter writes
        (UTC for an aware value, naive local wall-clock time for a naive one).

        Args:
            value: Stored datetime text.
            microseconds: Whole microseconds to add (or subtract, for a negative ``sign``).
            sign: 1 to add, -1 to subtract.

        Returns:
            The shifted datetime text, or ``None`` when either operand is NULL.
        """
        if value is None or microseconds is None:
            return None
        delta = datetime.timedelta(microseconds=sign * microseconds)
        parsed = datetime.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return Timezone.get_system_local_naive(cls.get_aware_datetime(value) + delta).isoformat(" ")
        return (parsed + delta).astimezone(datetime.UTC).isoformat(" ")

    @staticmethod
    def shift_date(value: str | None, microseconds: int | None, sign: int) -> str | None:
        """Backs ``SQLITE_DATE_SHIFT_FUNCTION_NAME`` - the date, taken as midnight, moved by
        ``sign * microseconds`` and truncated back to a date (floor).

        Args:
            value: Stored date text.
            microseconds: Whole microseconds to add (or subtract, for a negative ``sign``).
            sign: 1 to add, -1 to subtract.

        Returns:
            The shifted date as ``YYYY-MM-DD``, or ``None`` when either operand is NULL.
        """
        if value is None or microseconds is None:
            return None
        midnight = datetime.datetime.fromisoformat(value).replace(hour=0, minute=0, second=0, microsecond=0)
        return (midnight + datetime.timedelta(microseconds=sign * microseconds)).date().isoformat()

    @classmethod
    def difference_datetime(cls, left: str | None, right: str | None) -> int | None:
        """Backs ``SQLITE_DATETIME_DIFFERENCE_FUNCTION_NAME``.

        Args:
            left: Stored datetime text of the minuend.
            right: Stored datetime text of the subtrahend.

        Returns:
            ``left - right`` in whole microseconds, or ``None`` when either operand is NULL.
        """
        if left is None or right is None:
            return None
        return TimeDeltaField.get_microseconds(cls.get_aware_datetime(left) - cls.get_aware_datetime(right))

    @staticmethod
    def difference_date(left: str | None, right: str | None) -> int | None:
        """Backs ``SQLITE_DATE_DIFFERENCE_FUNCTION_NAME``.

        Args:
            left: Stored date text of the minuend.
            right: Stored date text of the subtrahend.

        Returns:
            ``left - right`` in whole microseconds (a whole number of days), or ``None`` when
            either operand is NULL.
        """
        if left is None or right is None:
            return None
        left_date = datetime.datetime.fromisoformat(left).date()
        right_date = datetime.datetime.fromisoformat(right).date()
        return (left_date - right_date).days * MICROSECONDS_PER_DAY

    @staticmethod
    async def install(connection: aiosqlite.Connection) -> None:
        """Registers the date/datetime shift and difference UDFs on `connection` - always installed
        (see `SqliteClient._post_connect`), since they back `F("dt") + timedelta(...)`-style
        arithmetic that every other dialect supports natively.

        Args:
            connection: The SQLite connection to register the functions on.
        """
        functions: Any = TemporalArithmeticFunctions
        native_functions = SqliteNativeFunctions.module
        if native_functions is not None:
            functions = native_functions.TemporalArithmetic(TemporalArithmeticFunctions)
        await connection.create_function(SQLITE_DATETIME_SHIFT_FUNCTION_NAME, 3, functions.shift_datetime)
        await connection.create_function(SQLITE_DATE_SHIFT_FUNCTION_NAME, 3, functions.shift_date)
        await connection.create_function(SQLITE_DATETIME_DIFFERENCE_FUNCTION_NAME, 2, functions.difference_datetime)
        await connection.create_function(SQLITE_DATE_DIFFERENCE_FUNCTION_NAME, 2, functions.difference_date)
