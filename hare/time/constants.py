from __future__ import annotations

import datetime
import sys

#: Timezone name used when none is explicitly configured.
DEFAULT_TIMEZONE = "UTC"

#: The moment whose system-local UTC offset stands in for the system zone when neither
#: `datetime.astimezone()` (before 1970 on Windows) nor the optional `tzlocal` can name it.
SYSTEM_ZONE_FALLBACK_REFERENCE_MOMENT = datetime.datetime(1970, 1, 2, tzinfo=datetime.UTC)

#: How many parsed zones ``Timezone.parse()`` keeps.
ZONE_CACHE_SIZE = 256

#: Whether ``datetime.now()`` reads the system's precise wall clock - on Windows before Python 3.13
#: it reads one that ticks every few milliseconds.
DATETIME_NOW_IS_PRECISE = sys.platform != "win32" or sys.version_info >= (3, 13)
#: 100-nanosecond FILETIME ticks between 1601-01-01 and the POSIX epoch.
FILETIME_POSIX_EPOCH_TICKS = 116_444_736_000_000_000
#: FILETIME ticks in a second, and in a microsecond.
FILETIME_TICKS_PER_SECOND = 10_000_000
FILETIME_TICKS_PER_MICROSECOND = 10
