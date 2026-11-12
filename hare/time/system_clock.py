from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import ClassVar

from hare.native.native_modules import NativeModules
from hare.time.constants import DATETIME_NOW_IS_PRECISE


class SystemClock:
    """The system's wall clock at full precision, shared by the moments hare stamps.

    System clock adjustments can move timestamps backwards; use a monotonic clock for durations.

    ``rust.native.clock`` reads the precise clock - where ``datetime.now()`` reads a coarser one
    (Windows before Python 3.13) as well.
    """

    #: The current moment in UTC, aware - rust.native's read is the fastest one.
    get_utc_now: ClassVar[Callable[[], datetime]] = NativeModules.clock.get_utc_now

    # The current moment in the system's local time, naive.
    if DATETIME_NOW_IS_PRECISE:
        get_local_now: ClassVar[Callable[[], datetime]] = datetime.now
    else:

        @staticmethod
        def get_local_now() -> datetime:
            """The current moment in the system's local time, naive."""
            seconds, microseconds = NativeModules.clock.get_posix_time()
            return datetime.fromtimestamp(seconds).replace(microsecond=microseconds)
