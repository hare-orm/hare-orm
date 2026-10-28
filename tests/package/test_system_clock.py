"""SystemClock is the one clock hare stamps moments with - on every Python version and platform it
reads the system's precise wall clock."""

import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

from hare.native.native_modules import NativeModules
from hare.time.system_clock import SystemClock

pytestmark = pytest.mark.database_independent

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


@pytest.mark.timing
@pytest.mark.requires("native")
def test_native_clock_reads_the_same_moment_as_python():
    native_utc = NativeModules.clock.get_utc_now()
    assert native_utc.tzinfo is UTC
    assert abs(native_utc - datetime.now(UTC)) < CLOCK_AGREEMENT
    seconds, microseconds = NativeModules.clock.get_posix_time()
    assert 0 <= microseconds < 1_000_000
    assert abs(seconds + microseconds / 1_000_000 - time.time()) < CLOCK_AGREEMENT.total_seconds()


@pytest.mark.timing
@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own precise clock")
def test_windows_precise_clock_reads_posix_time():
    seconds, microseconds = SystemClock.get_windows_posix_time()
    assert 0 <= microseconds < 1_000_000
    assert abs(seconds + microseconds / 1_000_000 - time.time()) < CLOCK_AGREEMENT.total_seconds()


def test_stamps_never_go_back():
    stamps = [SystemClock.get_utc_now() for _ in range(5000)]
    assert all(earlier <= later for earlier, later in zip(stamps, stamps[1:]))


@pytest.mark.timing
def test_stamps_advance_within_a_millisecond():
    """The clock is precise: it moves on within a millisecond, not once per coarse tick. The smallest step
    of many is taken - the process may be descheduled between any two readings."""
    steps: list[timedelta] = []
    deadline = time.monotonic() + 1
    while len(steps) < 100 and time.monotonic() < deadline:
        first = later = SystemClock.get_utc_now()
        while later == first and time.monotonic() < deadline:
            later = SystemClock.get_utc_now()
        if later > first:
            steps.append(later - first)
    assert steps
    assert min(steps) <= timedelta(milliseconds=1)
