from __future__ import annotations

import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_EXTRACT_FUNCTION_NAME, TIME_TEXT_FORMAT
from hare.dialects.sqlite.functions.constants import DATE_PART_EXTRACTORS
from hare.dialects.sqlite.functions.datetime.date_truncation import DateTruncation
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.sql.enums import DatetimeCastTarget
from hare.time import Timezone


class SqliteDatePartExtraction:
    """SQLite's user-defined function extracting a part of a date, time or datetime text."""

    #: Keeps the owner bucket of ``Timezone.ZONES`` the native function reads its zones from.
    cache_buckets: ClassVar[dict[int, Any]] = {}

    @staticmethod
    def extract(date_part: str, value: str | None, zone_name: str | None) -> int | str | None:
        """Backs ``SQLITE_EXTRACT_FUNCTION_NAME``: a date part of a stored value.

        Args:
            date_part: The ``DatePart`` name.
            value: The stored ISO text - a datetime (UTC with ``+00:00`` under ``use_timezone=True``,
                naive local time otherwise), a date or a time.
            zone_name: The zone to extract in - set only for an aware datetime.
        """
        if value is None:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(value)
        except ValueError:
            time_value = datetime.time.fromisoformat(value)
            parsed = datetime.datetime.combine(datetime.date.min, time_value.replace(tzinfo=None))
        if zone_name:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.UTC)
            parsed = parsed.astimezone(ZoneInfo(zone_name))
        if date_part == DatetimeCastTarget.DATE:
            return parsed.date().isoformat()
        if date_part == DatetimeCastTarget.TIME:
            return TIME_TEXT_FORMAT.format(parsed)
        try:
            extractor = DATE_PART_EXTRACTORS[date_part]
        except KeyError:
            raise ValueError(f"Unsupported date part: {date_part!r}") from None
        return extractor(parsed)

    @staticmethod
    async def install(connection: aiosqlite.Connection) -> None:
        """Registers ``SqliteDatePartExtraction.extract`` on ``connection`` as
        ``SQLITE_EXTRACT_FUNCTION_NAME``.
        """
        native_functions = SqliteNativeFunctions.module
        extract: Any = SqliteDatePartExtraction.extract
        if native_functions is not None:
            extract = native_functions.DateFunctions(
                ZoneInfo,
                Timezone.ZONES.get_owner_bucket(SqliteDatePartExtraction),
                SqliteDatePartExtraction.extract,
                DateTruncation.truncate,
            ).extract
        await connection.create_function(SQLITE_EXTRACT_FUNCTION_NAME, 3, extract)
