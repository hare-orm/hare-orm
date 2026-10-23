from __future__ import annotations

import datetime


class PreEpochLocalZone(datetime.tzinfo):
    """The system zone's offset attached to a naive datetime before 1970, which asyncpg can't read as
    local time on Windows - a ``timestamptz`` takes the moment it names, a ``timestamp`` the wall-clock
    time it was given, with the zone dropped again.

    Args:
        offset: The system zone's offset at that time.
    """

    __slots__ = ("offset",)

    def __init__(self, offset: datetime.timedelta | None) -> None:
        self.offset = offset

    def utcoffset(self, dt: datetime.datetime | None) -> datetime.timedelta | None:
        return self.offset

    def dst(self, dt: datetime.datetime | None) -> datetime.timedelta | None:
        return None

    def tzname(self, dt: datetime.datetime | None) -> str | None:
        return None
