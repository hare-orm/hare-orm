from __future__ import annotations

from hare.instrumentation.pools.duration_records import DurationRecords


class PoolStatistics:
    """What a client's pool of connections has done - kept by the client, so a reconnect, which
    replaces the pool, keeps it. A driver whose pool lives outside Python (the Rust one) keeps an
    object of its own with the same methods.

    The counters taking a connection changes are plain attributes, changed in place without a call;
    a wait for a connection is measured only while the pool metrics are enabled (``PoolMetrics``).
    """

    __slots__ = (
        "acquiring",
        "acquire_count",
        "acquire_timeouts",
        "acquire_wait_seconds_total",
        "connect_count",
        "connect_failures",
        "wait_records",
        "connect_records",
    )

    def __init__(self) -> None:
        #: The tasks taking a connection right now - waiting for one when the pool has none free.
        self.acquiring = 0
        self.acquire_count = 0
        self.acquire_timeouts = 0
        self.acquire_wait_seconds_total = 0.0
        self.connect_count = 0
        self.connect_failures = 0
        self.wait_records = DurationRecords()
        self.connect_records = DurationRecords()

    def add_wait(self, seconds: float) -> None:
        """Counts a wait for a connection.

        Args:
            seconds: How long it took.
        """
        self.acquire_wait_seconds_total += seconds
        self.wait_records.add(seconds)

    def add_timeout(self) -> None:
        """Counts a wait for a connection that ran out of ``pool_acquire_timeout``."""
        self.acquire_timeouts += 1

    def add_connect(self, seconds: float) -> None:
        """Counts an opened connection.

        Args:
            seconds: How long opening it took.
        """
        self.connect_count += 1
        self.connect_records.add(seconds)

    def add_connect_failure(self) -> None:
        """Counts a failure to open a connection."""
        self.connect_failures += 1

    def get_counts(self) -> tuple[int, int, float, int, int]:
        """The counters.

        Returns:
            The connections taken, the timeouts, the time waited in all, the connections opened
            and the failures to open one.
        """
        return (
            self.acquire_count,
            self.acquire_timeouts,
            self.acquire_wait_seconds_total,
            self.connect_count,
            self.connect_failures,
        )

    def get_waits_since(self, cursor: int) -> tuple[int, list[float], int]:
        """The waits for a connection after a reader's cursor - see ``DurationRecords.get_since()``."""
        return self.wait_records.get_since(cursor)

    def get_connects_since(self, cursor: int) -> tuple[int, list[float], int]:
        """The connects after a reader's cursor - see ``DurationRecords.get_since()``."""
        return self.connect_records.get_since(cursor)
