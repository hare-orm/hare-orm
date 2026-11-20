from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from hare.dialects.sqlite.constants import TIME_TEXT_FORMAT
from hare.dialects.sqlite.functions.constants import DATE_PART_EXTRACTORS, DATE_TEXT_LENGTH
from hare.sql.enums import DatetimeCastTarget
from hare.time.system_zone import SystemZone


class SqliteDatePartExtraction:
    """SQLite's user-defined function extracting a part of a date, time or datetime text."""

    @staticmethod
    def extract(date_part: str, value: str | None, zone_name: str | None) -> int | str | None:
        """Backs ``SQLITE_EXTRACT_FUNCTION_NAME``: a date part of a stored value.

        Args:
            date_part: The ``DatePart`` name.
            value: The stored ISO text - a datetime (UTC with ``+00:00`` under ``use_timezone=True``,
                naive local time otherwise), a date or a time.
            zone_name: The zone to extract in - a naive datetime is the system's local wall clock.
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
                parsed = (
                    SystemZone.make_system_local_aware(parsed)
                    if len(value) > DATE_TEXT_LENGTH
                    else parsed.replace(tzinfo=datetime.UTC)
                )
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
