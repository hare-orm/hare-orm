from __future__ import annotations

from zoneinfo import ZoneInfo as _ZoneInfo


class ZoneInfo(_ZoneInfo):
    """A time zone of ``zoneinfo``, with ``zone`` naming it as its key."""

    @property
    def zone(self) -> str:
        # Compatible with pytz:
        # >>> ZoneInfo('UTC').key == pytz.timezone('UTC').zone == 'UTC'
        return self.key
