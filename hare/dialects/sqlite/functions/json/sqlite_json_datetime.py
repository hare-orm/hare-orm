from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_JSON_DATETIME_FUNCTION_NAME
from hare.dialects.sqlite.functions.constants import DATE_PART_EXTRACTORS, SQLITE_JSON_DATETIME_EPOCH
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.lazy_loading.lazy_pattern import LazyPattern
from hare.query.filters.constants import JSON_ISO_DATETIME_PATTERN
from hare.sql.constants import MICROSECONDS_PER_DAY, MICROSECONDS_PER_SECOND


class SqliteJsonDatetime:
    """Reads an ISO-8601 date/datetime string the way a JSON ``__filter`` date comparison casts it
    on Postgres, as whole microseconds since the Unix epoch: ``wall`` mode as ``timestamp`` (the
    wall-clock digits, any offset ignored), ``instant`` mode as ``timestamptz`` (no offset means
    UTC). A ``DatePart`` name as the mode gives that part of the wall-clock value. Fractional
    seconds round to microseconds, which can carry ``9999-12-31T23:59:59.9999999`` into year 10000
    as on Postgres."""

    ISO_DATETIME_PATTERN = LazyPattern(JSON_ISO_DATETIME_PATTERN)

    @staticmethod
    def get_offset_microseconds(offset_text: str) -> int:
        """Microseconds east of UTC of an offset like ``Z``, ``+05``, ``-0530`` or ``+05:30``."""
        if offset_text == "Z":
            return 0
        sign = -1 if offset_text[0] == "-" else 1
        digits = offset_text[1:].replace(":", "")
        minutes = int(digits[:2]) * 60 + (int(digits[2:4]) if len(digits) > 2 else 0)
        return sign * minutes * 60 * MICROSECONDS_PER_SECOND

    @staticmethod
    def get_wall_microseconds(day: datetime.date, clock_microseconds: int) -> int:
        """Microseconds since the epoch of a wall-clock day and time of day."""
        return (day.toordinal() - SQLITE_JSON_DATETIME_EPOCH.toordinal()) * MICROSECONDS_PER_DAY + clock_microseconds

    @classmethod
    def parse(cls, text: str) -> tuple[int, int | None] | None:
        """The wall-clock microseconds of an ISO text and its UTC offset in microseconds, or
        ``None`` for text Postgres wouldn't cast.

        Returns:
            ``(wall_microseconds, offset_microseconds_or_None)``, or ``None``.
        """
        if not cls.ISO_DATETIME_PATTERN.match(text):
            return None
        try:
            day = datetime.date.fromisoformat(text[:10])
        except ValueError:
            return None
        time_text = text[11:]
        offset_microseconds = None
        if time_text.endswith("Z"):
            offset_microseconds, time_text = 0, time_text[:-1]
        else:
            sign_index = max(time_text.rfind("+"), time_text.rfind("-"))
            if sign_index > 0:
                offset_microseconds = cls.get_offset_microseconds(time_text[sign_index:])
                time_text = time_text[:sign_index]
        clock_microseconds = 0
        if time_text:
            clock_parts = time_text.split(":")
            seconds = Decimal(clock_parts[2]) if len(clock_parts) > 2 else Decimal(0)
            clock_microseconds = (int(clock_parts[0]) * 60 + int(clock_parts[1])) * 60 * MICROSECONDS_PER_SECOND + int(
                round(seconds * MICROSECONDS_PER_SECOND)
            )
        return cls.get_wall_microseconds(day, clock_microseconds), offset_microseconds

    @staticmethod
    def get_date_part(wall_microseconds: int, date_part: str) -> int:
        """One ``DatePart`` of a wall-clock value, as ``EXTRACT()`` gives it."""
        days, clock_microseconds = divmod(wall_microseconds, MICROSECONDS_PER_DAY)
        ordinal = SQLITE_JSON_DATETIME_EPOCH.toordinal() + days
        if ordinal <= datetime.date.max.toordinal():
            moment = datetime.datetime.fromordinal(ordinal) + datetime.timedelta(microseconds=clock_microseconds)
            return DATE_PART_EXTRACTORS[date_part](moment)
        # Only 10000-01-01 00:00:00 is reachable past `date.max`, by rounding up its last microsecond.
        last_day = datetime.date.max
        first_weekday = last_day.isoweekday() % 7 + 1
        first_day_parts = {
            "YEAR": last_day.year + 1,
            "QUARTER": 1,
            "MONTH": 1,
            "WEEK": 1 if first_weekday <= 4 else last_day.isocalendar().week,
            "DAY": 1,
        }
        return first_day_parts.get(date_part, 0)

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
        native_functions = SqliteNativeFunctions.module
        normalize: Any = cls.normalize if native_functions is None else native_functions.JsonDatetime(cls).normalize
        await connection.create_function(SQLITE_JSON_DATETIME_FUNCTION_NAME, 2, normalize, deterministic=True)
