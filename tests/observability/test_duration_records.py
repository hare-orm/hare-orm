"""The latest durations a pool measured (``DurationRecords``), each reader taking the ones after its
own cursor - the Python records and the Rust driver's (``pool.PoolStatistics``) alike."""

import pytest

from hare.dialects.base.client.pool.pool_statistics import PoolStatistics
from hare.instrumentation.pools.duration_records import DurationRecords
from hare.instrumentation.pools.sample_window import SampleWindow

pytestmark = pytest.mark.database_independent


def get_statistics_classes():
    classes = [PoolStatistics]
    try:
        from rust.native import pool
    except ImportError:
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


@pytest.mark.parametrize("make_statistics", get_statistics_classes())
def test_completed_events_are_not_limited_by_sample_capacity(make_statistics):
    statistics = make_statistics(2)
    first = []
    statistics.set_recorder(lambda event, value: first.append((event, value)))
    for _ in range(100):
        statistics.add_wait(0.25)
        statistics.add_connect(0.5)
    statistics.add_timeout()
    statistics.add_connect_failure()
    assert first == [("wait", 0.25), ("connect", 0.5)] * 100 + [("timeout", 1), ("connect_failure", 1)]
    window = SampleWindow.from_records(0, statistics.get_waits_since(0))
    assert (window.observations, window.missed, window.values) == (100, 98, (0.25, 0.25))
    assert not window.complete
    # Reading samples does not consume another reader's observations or any completed event.
    assert SampleWindow.from_records(0, statistics.get_waits_since(0)) == window
    assert SampleWindow.from_records(100, statistics.get_waits_since(100)).observations == 0
    statistics.set_recorder(None)
    statistics.add_timeout()
    assert len(first) == 202
