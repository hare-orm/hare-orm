"""SystemClock is the one clock hare stamps moments with - on every Python version and platform it
reads the system's precise wall clock."""

import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

from hare.native.native_modules import NativeModules
from hare.time.system_clock import SystemClock

#: How far a stamp may be from ``datetime.now()`` read right next to it - Python's own clock on
#: Windows before 3.13 lags the precise one by up to a tick (about 16 ms).
CLOCK_AGREEMENT = timedelta(milliseconds=50)


def test_utc_now_is_an_aware_utc_moment_of_now():
    stamped = SystemClock.get_utc_now()
    assert stamped.tzinfo is UTC
    assert abs(stamped - datetime.now(UTC)) < CLOCK_AGREEMENT


def test_local_now_is_a_naive_moment_of_the_local_wall_clock():
    stamped = SystemClock.get_local_now()
    assert stamped.tzinfo is None
    assert abs(stamped - datetime.now()) < CLOCK_AGREEMENT


@pytest.mark.skipif(NativeModules.clock is None, reason="rust.native isn't built")
def test_native_clock_reads_the_same_moment_as_python():
    native_utc = NativeModules.clock.get_utc_now()
    assert native_utc.tzinfo is UTC
    assert abs(native_utc - datetime.now(UTC)) < CLOCK_AGREEMENT
    seconds, microseconds = NativeModules.clock.get_posix_time()
    assert 0 <= microseconds < 1_000_000
    assert abs(seconds + microseconds / 1_000_000 - time.time()) < CLOCK_AGREEMENT.total_seconds()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own precise clock")
def test_windows_precise_clock_reads_posix_time():
    seconds, microseconds = SystemClock.get_windows_posix_time()
    assert 0 <= microseconds < 1_000_000
    assert abs(seconds + microseconds / 1_000_000 - time.time()) < CLOCK_AGREEMENT.total_seconds()


def test_stamps_never_go_back():
    stamps = [SystemClock.get_utc_now() for _ in range(5000)]
    assert all(earlier <= later for earlier, later in zip(stamps, stamps[1:]))


def test_stamps_advance_within_a_millisecond():
    """The clock is precise: it moves on within a millisecond, not once per coarse tick."""
    first = SystemClock.get_utc_now()
    later = first
    deadline = first + timedelta(milliseconds=1)
    while later == first and SystemClock.get_utc_now() <= deadline:
        later = SystemClock.get_utc_now()
    assert later > first
    assert later - first <= timedelta(milliseconds=1)
