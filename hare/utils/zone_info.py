from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo as _ZoneInfo

if TYPE_CHECKING:
    pass


class ZoneInfo(_ZoneInfo):
    @property
    def zone(self) -> str:
        # Compatible with pytz:
        # >>> ZoneInfo('UTC').key == pytz.timezone('UTC').zone == 'UTC'
        return self.key
