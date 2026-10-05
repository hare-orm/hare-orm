from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any, ClassVar

from hare.native.native_modules import NativeModules
from hare.time.constants import (
    DATETIME_NOW_IS_PRECISE,
    FILETIME_POSIX_EPOCH_TICKS,
    FILETIME_TICKS_PER_MICROSECOND,
    FILETIME_TICKS_PER_SECOND,
)


class SystemClock:
    """The system's wall clock at full precision - the one source of every moment hare stamps, so
    a stamp taken after another never comes out before it.

    Where ``datetime.now()`` reads a coarser clock (Windows before Python 3.13) the precise one is
    read by ``rust.native.clock``, or by ``kernel32.GetSystemTimePreciseAsFileTime`` without the
    extension.
    """

    #: ``kernel32.GetSystemTimePreciseAsFileTime``, ``ctypes.c_ulonglong`` and ``ctypes.byref``,
    #: bound by the first ``get_windows_posix_time()``.
    get_system_time_precise_as_file_time: ClassVar[Callable[[Any], Any] | None] = None
    file_time_type: ClassVar[Callable[[], Any] | None] = None
    byref: ClassVar[Callable[[Any], Any] | None] = None

    # The current moment in UTC, aware - rust.native's read is the fastest one.
    if NativeModules.clock is not None:
        get_utc_now: ClassVar[Callable[[], datetime]] = NativeModules.clock.get_utc_now
    elif DATETIME_NOW_IS_PRECISE:  # pragma: nocoverage - without rust.native
        get_utc_now = partial(datetime.now, UTC)
    else:  # pragma: nocoverage - Windows before Python 3.13, without rust.native

        @staticmethod
        def get_utc_now() -> datetime:
            """The current moment in UTC, aware."""
            seconds, microseconds = SystemClock.get_windows_posix_time()
            return datetime.fromtimestamp(seconds, UTC).replace(microsecond=microseconds)

    # The current moment in the system's local time, naive.
    if DATETIME_NOW_IS_PRECISE:
        get_local_now: ClassVar[Callable[[], datetime]] = datetime.now
    elif NativeModules.clock is not None:  # pragma: nocoverage - Windows before Python 3.13

        @staticmethod
        def get_local_now() -> datetime:
            """The current moment in the system's local time, naive."""
            seconds, microseconds = NativeModules.clock.get_posix_time()
            return datetime.fromtimestamp(seconds).replace(microsecond=microseconds)
    else:  # pragma: nocoverage - Windows before Python 3.13, without rust.native

        @staticmethod
        def get_local_now() -> datetime:
            """The current moment in the system's local time, naive."""
            seconds, microseconds = SystemClock.get_windows_posix_time()
            return datetime.fromtimestamp(seconds).replace(microsecond=microseconds)

    @classmethod
    def get_windows_posix_time(cls) -> tuple[int, int]:  # pragma: nocoverage - Windows only
        """The POSIX time from Windows' precise system clock.

        Returns:
            Whole seconds and the microseconds past them.
        """
        get_file_time = cls.get_system_time_precise_as_file_time
        file_time_type = cls.file_time_type
        byref = cls.byref
        if get_file_time is None or file_time_type is None or byref is None:
            import ctypes

            get_file_time = cls.get_system_time_precise_as_file_time = getattr(
                ctypes, "windll"
            ).kernel32.GetSystemTimePreciseAsFileTime
            file_time_type = cls.file_time_type = ctypes.c_ulonglong
            byref = cls.byref = ctypes.byref
        file_time = file_time_type()
        get_file_time(byref(file_time))
        seconds, remaining_ticks = divmod(file_time.value - FILETIME_POSIX_EPOCH_TICKS, FILETIME_TICKS_PER_SECOND)
        return seconds, remaining_ticks // FILETIME_TICKS_PER_MICROSECOND
