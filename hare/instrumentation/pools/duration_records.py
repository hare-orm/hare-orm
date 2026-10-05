from __future__ import annotations

import threading

from hare.instrumentation.constants import DURATION_RECORDS_CAPACITY


class DurationRecords:
    """The latest durations of one type a pool measured - its waits for a connection, or its
    connects - numbered in the order they came. Each reader (the OpenTelemetry metrics, each health
    check) keeps its own cursor and takes the durations after it, so readers never take them from
    one another. Read from the metrics exporter's thread too, hence the lock.

    Args:
        capacity: How many of the latest durations are kept.
    """

    __slots__ = ("capacity", "durations", "count", "lock")

    def __init__(self, capacity: int = DURATION_RECORDS_CAPACITY) -> None:
        self.capacity = capacity
        self.durations = [0.0] * capacity
        #: How many durations were ever added - the cursor of a reader that has them all.
        self.count = 0
        self.lock = threading.Lock()

    def add(self, seconds: float) -> None:
        """Adds a duration.

        Args:
            seconds: The duration.
        """
        with self.lock:
            self.durations[self.count % self.capacity] = seconds
            self.count += 1

    def get_since(self, cursor: int) -> tuple[int, list[float], int]:
        """The durations added after a cursor.

        Args:
            cursor: The reader's cursor - 0 for a reader that has none yet.

        Returns:
            The reader's new cursor, the durations, and how many were already overwritten.
        """
        with self.lock:
            count = self.count
            first = max(cursor, count - self.capacity)
            durations = [self.durations[number % self.capacity] for number in range(first, count)]
        return count, durations, first - cursor
