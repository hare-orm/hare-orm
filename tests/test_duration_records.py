"""The latest durations a pool measured (``DurationRecords``), each reader taking the ones after its
own cursor - the Python records and the Rust driver's (``pool.PoolStatistics``) alike."""

import pytest

from hare.dialects.base.client.pool.pool_statistics import PoolStatistics
from hare.instrumentation.pools.duration_records import DurationRecords


def get_statistics_classes():
    classes = [lambda capacity: PoolStatistics()]
    try:
        from rust.native import pool
    except ImportError:  # pragma: nocoverage - the native extension is not built
        return classes
    return [*classes, pool.PoolStatistics]


def test_two_readers_take_the_durations_after_their_own_cursors():
    records = DurationRecords(capacity=4)
    records.add(1.0)
    first_cursor, durations, skipped = records.get_since(0)
    assert (first_cursor, durations, skipped) == (1, [1.0], 0)
    records.add(2.0)
    assert records.get_since(first_cursor) == (2, [2.0], 0)
    assert records.get_since(0) == (2, [1.0, 2.0], 0)


def test_a_reader_behind_the_capacity_gets_the_count_it_missed():
    records = DurationRecords(capacity=2)
    for seconds in (1.0, 2.0, 3.0, 4.0, 5.0):
        records.add(seconds)
    assert records.get_since(0) == (5, [4.0, 5.0], 3)
    assert records.get_since(9) == (5, [], 0)


@pytest.mark.parametrize("make_statistics", get_statistics_classes())
def test_the_statistics_of_every_driver_count_alike(make_statistics):
    statistics = make_statistics(4)
    statistics.add_wait(0.25)
    statistics.add_timeout()
    statistics.add_connect_failure()
    acquire_count, timeouts, wait_total, _, connect_failures = statistics.get_counts()
    assert (acquire_count, timeouts, connect_failures) == (0, 1, 1)
    assert wait_total == pytest.approx(0.25)
    cursor, waits, skipped = statistics.get_waits_since(0)
    assert (cursor, waits, skipped) == (1, [pytest.approx(0.25)], 0)
    assert statistics.get_waits_since(cursor) == (1, [], 0)
    assert statistics.get_connects_since(0) == (0, [], 0)
