import datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from hare.dialects.sqlite.constants import (
    DATE_TEXT_LENGTH,
    SQLITE_TRUNC_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.sql.enums import TruncType


class DateTruncation:
    """Backs ``SQLITE_TRUNC_FUNCTION_NAME`` (see ``DateTrunc``) - SQLite has no ``DATE_TRUNC()``."""

    @staticmethod
    def truncate_date(trunc_type: str, value: datetime.date) -> datetime.date:
        """A date truncated to a calendar type (``year``/``quarter``/``month``/``week``/``day``)."""
        if trunc_type == TruncType.YEAR:
            return value.replace(month=1, day=1)
        if trunc_type == TruncType.QUARTER:
            return value.replace(month=(value.month - 1) // 3 * 3 + 1, day=1)
        if trunc_type == TruncType.MONTH:
            return value.replace(day=1)
        if trunc_type == TruncType.WEEK:
            return value - datetime.timedelta(days=value.weekday())
        return value

    @staticmethod
    def truncate_time(trunc_type: str, value: datetime.time) -> datetime.time:
        """A time of day truncated to ``hour``/``minute``/``second``, keeping its offset."""
        if trunc_type == TruncType.HOUR:
            return value.replace(minute=0, second=0, microsecond=0)
        if trunc_type == TruncType.MINUTE:
            return value.replace(second=0, microsecond=0)
        if trunc_type == TruncType.SECOND:
            return value.replace(microsecond=0)
        return value

    @classmethod
    def truncate(cls, trunc_type: str, value: str | None, zone_name: str | None) -> str | None:
        """Truncates a stored date, time or datetime text.

        Args:
            trunc_type: A ``TruncType`` value.
            value: The stored ISO text.
            zone_name: The zone an aware datetime is truncated in.

        Returns:
            The truncated value as the text its column type stores (an aware datetime in UTC), a
            datetime's date or time of day for the ``date``/``time`` types, or ``None``.
        """
        if value is None:
            return None
        if len(value) == DATE_TEXT_LENGTH:
            return cls.truncate_date(trunc_type, datetime.date.fromisoformat(value)).isoformat()
        try:
            moment = datetime.datetime.fromisoformat(value)
        except ValueError:
            return cls.truncate_time(trunc_type, datetime.time.fromisoformat(value)).isoformat()
        zone = ZoneInfo(zone_name) if zone_name and moment.tzinfo is not None else None
        local_moment = moment.astimezone(zone) if zone is not None else moment
        if trunc_type == TruncType.DATE:
            return local_moment.date().isoformat()
        if trunc_type == TruncType.TIME:
            return local_moment.time().isoformat()
        if trunc_type in (TruncType.HOUR, TruncType.MINUTE, TruncType.SECOND):
            wall_clock = datetime.datetime.combine(
                local_moment.date(), cls.truncate_time(trunc_type, local_moment.time().replace(tzinfo=None))
            )
        else:
            wall_clock = datetime.datetime.combine(cls.truncate_date(trunc_type, local_moment.date()), datetime.time())
        if zone is None:
            return wall_clock.replace(tzinfo=moment.tzinfo).isoformat(" ")
        return wall_clock.replace(tzinfo=zone).astimezone(datetime.UTC).isoformat(" ")

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers ``truncate`` on ``connection`` as ``SQLITE_TRUNC_FUNCTION_NAME``."""
        native_functions = SqliteNativeFunctions.module
        truncate: Any = cls.truncate
        if native_functions is not None:
            # Imported here: the module imports this one.
            from hare.dialects.sqlite.functions.datetime.sqlite_date_part_extraction import (
                SqliteDatePartExtraction,
            )

            truncate = native_functions.DateFunctions(
                ZoneInfo, SqliteDatePartExtraction.extract, cls.truncate
            ).truncate
        await connection.create_function(SQLITE_TRUNC_FUNCTION_NAME, 3, truncate, deterministic=True)
