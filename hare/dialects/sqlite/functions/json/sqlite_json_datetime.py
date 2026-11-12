from __future__ import annotations

import datetime
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_JSON_DATETIME_FUNCTION_NAME
from hare.dialects.sqlite.functions.constants import DATE_PART_EXTRACTORS, SQLITE_JSON_DATETIME_EPOCH
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.lazy_loading.lazy_pattern import LazyPattern
from hare.query.filters.constants import JSON_ISO_DATETIME_PATTERN
from hare.sql.constants import MICROSECONDS_PER_DAY, MICROSECONDS_PER_SECOND


class SqliteJsonDatetime:
    """Reads an ISO-8601 date/datetime string a JSON ``__filter`` date comparison compares, as whole
    microseconds since the Unix epoch - SQLite's own date functions read milliseconds only:
    ``wall`` mode as ``timestamp`` (the wall-clock digits, any offset ignored), ``instant`` mode as
    ``timestamptz`` (no offset means UTC). A ``DatePart`` name as the mode gives that part of the
    wall-clock value. Fractional seconds are cut at microseconds. The entire string must match
    the ISO form, without leading or trailing whitespace."""

    ISO_DATETIME_PATTERN = LazyPattern(JSON_ISO_DATETIME_PATTERN)

    @staticmethod
    def get_wall_microseconds(day: datetime.date, clock_microseconds: int) -> int:
        """Microseconds since the epoch of a wall-clock day and time of day."""
        return (day.toordinal() - SQLITE_JSON_DATETIME_EPOCH.toordinal()) * MICROSECONDS_PER_DAY + clock_microseconds

    @classmethod
    def parse(cls, text: str) -> tuple[int, int | None] | None:
        """The wall-clock microseconds of an ISO text and its UTC offset in microseconds, or
        ``None`` for text that isn't an ISO date/datetime.

        Returns:
            ``(wall_microseconds, offset_microseconds_or_None)``, or ``None``.
        """
        if not cls.ISO_DATETIME_PATTERN.fullmatch(text):
            return None
        try:
            moment = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        offset = moment.utcoffset()
        return cls.normalize_value(moment, "wall"), None if offset is None else TimeDeltaField.get_microseconds(offset)

    @staticmethod
    def get_date_part(wall_microseconds: int, date_part: str) -> int:
        """One ``DatePart`` of a wall-clock value, as ``EXTRACT()`` gives it."""
        days, clock_microseconds = divmod(wall_microseconds, MICROSECONDS_PER_DAY)
        ordinal = SQLITE_JSON_DATETIME_EPOCH.toordinal() + days
        moment = datetime.datetime.fromordinal(ordinal) + datetime.timedelta(microseconds=clock_microseconds)
        return DATE_PART_EXTRACTORS[date_part](moment)

    @classmethod
    def normalize(cls, text: Any, mode: str) -> int | None:
        """Backs ``SQLITE_JSON_DATETIME_FUNCTION_NAME``.

        Args:
            text: The string at the JSON path.
            mode: ``wall``, ``instant`` or a ``DatePart`` name.

        Returns:
            The microseconds or the date part, or ``None`` for anything that isn't an ISO
            date/datetime.
        """
        if not isinstance(text, str):
            return None
        parsed = cls.parse(text)
        if parsed is None:
            return None
        wall_microseconds, offset_microseconds = parsed
        if mode == "wall":
            return wall_microseconds
        if mode == "instant":
            return wall_microseconds - (offset_microseconds or 0)
        return cls.get_date_part(wall_microseconds, mode)

    @classmethod
    def normalize_value(cls, value: datetime.date, mode: str) -> int:
        """A date/datetime filter value in the form ``normalize`` gives the stored text."""
        if not isinstance(value, datetime.datetime):
            value = datetime.datetime.combine(value, datetime.time())
        clock_microseconds = (
            (value.hour * 60 + value.minute) * 60 + value.second
        ) * MICROSECONDS_PER_SECOND + value.microsecond
        wall_microseconds = cls.get_wall_microseconds(value.date(), clock_microseconds)
        offset = value.utcoffset()
        if mode == "wall" or offset is None:
            return wall_microseconds
        return wall_microseconds - offset // datetime.timedelta(microseconds=1)

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers ``normalize`` on ``connection`` as ``SQLITE_JSON_DATETIME_FUNCTION_NAME``."""
        normalize = SqliteNativeFunctions.module.JsonDatetime(cls).normalize
        await connection.create_function(SQLITE_JSON_DATETIME_FUNCTION_NAME, 2, normalize, deterministic=True)
