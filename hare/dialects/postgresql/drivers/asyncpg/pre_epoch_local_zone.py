from __future__ import annotations

import datetime


class PreEpochLocalZone(datetime.tzinfo):
    """The system zone's offset for a naive datetime whose local conversion fails near the range
    boundary. A ``timestamptz`` takes the moment it names; a ``timestamp`` takes the original
    wall-clock time, with this adapter's zone dropped again.

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
