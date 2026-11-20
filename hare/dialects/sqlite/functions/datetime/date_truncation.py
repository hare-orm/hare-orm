from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from hare.dialects.sqlite.functions.constants import DATE_TEXT_LENGTH
from hare.sql.enums import TruncType
from hare.time.system_zone import SystemZone


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
            zone_name: The zone a datetime is truncated in - a naive one is the system's local wall
                clock.

        Returns:
            The truncated value as the text its column type stores (an aware datetime in UTC, a
            naive one as the system's local wall clock), a datetime's date or time of day for the
            ``date``/``time`` types, or ``None``.
        """
        if value is None:
            return None
        if len(value) == DATE_TEXT_LENGTH:
            return cls.truncate_date(trunc_type, datetime.date.fromisoformat(value)).isoformat()
        try:
            moment = datetime.datetime.fromisoformat(value)
        except ValueError:
            return cls.truncate_time(trunc_type, datetime.time.fromisoformat(value)).isoformat()
        zone = ZoneInfo(zone_name) if zone_name else None
        is_naive = moment.tzinfo is None
        if zone is None:
            local_moment = moment
        else:
            local_moment = (SystemZone.make_system_local_aware(moment) if is_naive else moment).astimezone(zone)
        if trunc_type == TruncType.DATE:
            return local_moment.date().isoformat()
        if trunc_type == TruncType.TIME:
            return local_moment.time().isoformat()
        if trunc_type in {TruncType.HOUR, TruncType.MINUTE, TruncType.SECOND}:
            wall_clock = datetime.datetime.combine(
                local_moment.date(), cls.truncate_time(trunc_type, local_moment.time().replace(tzinfo=None))
            )
        else:
            wall_clock = datetime.datetime.combine(cls.truncate_date(trunc_type, local_moment.date()), datetime.time())
        if zone is None:
            return wall_clock.replace(tzinfo=moment.tzinfo).isoformat(" ")
        truncated = wall_clock.replace(tzinfo=zone)
        if is_naive:
            return SystemZone.get_system_local_naive(truncated).isoformat(" ")
        return truncated.astimezone(datetime.UTC).isoformat(" ")
