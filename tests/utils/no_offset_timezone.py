import datetime


class NoOffsetTimezone(datetime.tzinfo):
    """A valid tzinfo that leaves its datetime semantically naive."""

    def utcoffset(self, value: datetime.datetime | None) -> None:
        return None

    def dst(self, value: datetime.datetime | None) -> None:
        return None

    def tzname(self, value: datetime.datetime | None) -> None:
        return None
