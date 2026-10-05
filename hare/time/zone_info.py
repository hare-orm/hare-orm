from __future__ import annotations

from zoneinfo import ZoneInfo as _ZoneInfo


class ZoneInfo(_ZoneInfo):
    @property
    def zone(self) -> str:
        # Compatible with pytz:
        # >>> ZoneInfo('UTC').key == pytz.timezone('UTC').zone == 'UTC'
        return self.key
