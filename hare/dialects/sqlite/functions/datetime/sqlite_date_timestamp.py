from __future__ import annotations

import datetime
from typing import Any, ClassVar

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_DATE_TIMESTAMP_FUNCTION_NAME
from hare.dialects.sqlite.functions.constants import DATE_TEXT_LENGTH
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.time import Timezone
from hare.time.zone_names import ZoneNames


class SqliteDateTimestamp:
    """The stored text of a day's first moment, for comparing a date with a timestamp on SQLite."""

    #: Keeps the owner bucket of ``ZoneNames.ZONES`` the native function reads its zones from.
    cache_buckets: ClassVar[dict[int, Any]] = {}

    @staticmethod
    def get_start_text(value: Any, zone_name: str | None) -> str | None:
        """Backs ``SQLITE_DATE_TIMESTAMP_FUNCTION_NAME``.

        Args:
            value: The date's text.
            zone_name: The zone of an aware timestamp's day, None for a naive timestamp.

        Returns:
            The UTC text of the day's first moment in the zone, e.g. ``2020-01-01 15:00:00+00:00``;
            a naive midnight's text for no zone; ``None`` for NULL.
        """
        if value is None:
            return None
        day = datetime.date.fromisoformat(str(value).strip()[:DATE_TEXT_LENGTH])
        if zone_name is None:
            return datetime.datetime.combine(day, datetime.time.min).isoformat(" ")
        return Timezone.get_start_of_day(day, zone_name).astimezone(datetime.UTC).isoformat(" ")

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers ``get_start_text`` on ``connection``."""
        get_start_text = SqliteNativeFunctions.module.DateTimestamp(
            ZoneNames.parse, ZoneNames.ZONES.get_bucket(cls), cls.get_start_text
        ).get_start_text
        await connection.create_function(SQLITE_DATE_TIMESTAMP_FUNCTION_NAME, 2, get_start_text, deterministic=True)
