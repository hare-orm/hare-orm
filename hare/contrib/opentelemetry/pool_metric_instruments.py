from __future__ import annotations

import threading
import weakref
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from hare.contrib.opentelemetry.constants import (
    CONNECT_FAILURES_METRIC,
    CONNECTION_COUNT_METRIC,
    CONNECTION_CREATE_TIME_METRIC,
    CONNECTION_IDLE_MIN_METRIC,
    CONNECTION_MAX_METRIC,
    CONNECTION_PENDING_REQUESTS_METRIC,
    CONNECTION_POOL_NAME_ATTRIBUTE,
    CONNECTION_STATE_ATTRIBUTE,
    CONNECTION_TIMEOUTS_METRIC,
    CONNECTION_UNIT,
    CONNECTION_WAIT_TIME_METRIC,
    DB_SYSTEM_NAME_ATTRIBUTE,
    FAILURE_UNIT,
    IDLE_CONNECTION_STATE,
    POOL_ROLE_ATTRIBUTE,
    POOL_SCHEMA_ATTRIBUTE,
    REQUEST_UNIT,
    SECONDS_UNIT,
    TIMEOUT_UNIT,
    USED_CONNECTION_STATE,
)
from hare.instrumentation.pools.pool_metrics import PoolMetrics
from hare.instrumentation.pools.pool_registry import PoolRegistry

if TYPE_CHECKING:  # pragma: nocoverage
    from opentelemetry.metrics import MeterProvider

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.declarations import PoolStatus


class PoolMetricInstruments:
    """The OpenTelemetry metrics of the pools of connections - every open pool of the process
    (``PoolRegistry``), read when the metrics are collected, on the exporter's thread. Pools with
    the same attributes (one connection of several ``HareContext`` objects) are summed.

    A histogram can't be observed: the waits for a connection and the connects each pool measured
    (``DurationRecords``) are taken by a callback of the collection and recorded then - the histograms,
    made after the observed instruments, are collected after them and carry them.

    Args:
        meter_provider: The MeterProvider to get a meter from - the global one by default.
    """

    def __init__(self, meter_provider: MeterProvider | None = None) -> None:
        meter = metrics.get_meter(__name__, meter_provider=meter_provider)
        #: Whether the instruments report - set by ``start()``, cleared by ``stop()``; OpenTelemetry
        #: has no way to remove an instrument.
        self.started = False
        #: Each pool's cursors of its waits and connects - the pools are held weakly.
        self.cursors: weakref.WeakKeyDictionary[DatabaseClient, tuple[int, int]] = weakref.WeakKeyDictionary()
        #: Callbacks of one collection may run on several threads at once.
        self.lock = threading.Lock()
        meter.create_observable_up_down_counter(
            CONNECTION_COUNT_METRIC, [self.observe_connection_counts], unit=CONNECTION_UNIT
        )
        meter.create_observable_up_down_counter(CONNECTION_MAX_METRIC, [self.observe_max_sizes], unit=CONNECTION_UNIT)
        meter.create_observable_up_down_counter(
            CONNECTION_IDLE_MIN_METRIC, [self.observe_min_sizes], unit=CONNECTION_UNIT
        )
        meter.create_observable_up_down_counter(
            CONNECTION_PENDING_REQUESTS_METRIC, [self.observe_pending_requests], unit=REQUEST_UNIT
        )
        meter.create_observable_counter(CONNECTION_TIMEOUTS_METRIC, [self.observe_timeouts], unit=TIMEOUT_UNIT)
        meter.create_observable_counter(CONNECT_FAILURES_METRIC, [self.observe_connect_failures], unit=FAILURE_UNIT)
        self.wait_time = meter.create_histogram(CONNECTION_WAIT_TIME_METRIC, unit=SECONDS_UNIT)
        self.create_time = meter.create_histogram(CONNECTION_CREATE_TIME_METRIC, unit=SECONDS_UNIT)

    def start(self) -> None:
        """Starts reporting, and measuring the waits for a connection (``PoolMetrics``)."""
        if self.started:
            return
        PoolMetrics.enable()
        self.started = True

    def stop(self) -> None:
        """Stops reporting, and measuring the waits for this user."""
        if not self.started:
            return
        self.started = False
        PoolMetrics.disable()

    def get_pools(self) -> list[tuple[DatabaseClient, PoolStatus]]:
        """Every open pool reporting its status.

        Returns:
            Each pool's client and status - none while stopped.
        """
        if not self.started:
            return []
        pools = []
        for client in PoolRegistry.get_clients():
            if not client.features.supports_pool_status:
                continue
            status = client.get_pool_status()
            if status is not None:
                pools.append((client, status))
        return pools

    @staticmethod
    def get_attributes(client: DatabaseClient, status: PoolStatus) -> dict[str, str]:
        """The attributes of a pool's metrics.

        Args:
            client: The pool's client.
            status: The pool's status.

        Returns:
            The attributes.
        """
        attributes = {
            CONNECTION_POOL_NAME_ATTRIBUTE: status.connection_alias,
            POOL_ROLE_ATTRIBUTE: status.role.value,
            DB_SYSTEM_NAME_ATTRIBUTE: client.dialect.otel_system_name,
        }
        if status.schema is not None:
            attributes[POOL_SCHEMA_ATTRIBUTE] = status.schema
        return attributes

    @staticmethod
    def get_summed_observations(values: Iterable[tuple[dict[str, str], float]]) -> list[Observation]:
        """One observation per attribute set, the values of equal ones summed.

        Args:
            values: Each value with its attributes.

        Returns:
            The observations.
        """
        sums: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)
        for attributes, value in values:
            sums[tuple(sorted(attributes.items()))] += value
        return [Observation(value, dict(attributes)) for attributes, value in sums.items()]

    def observe_connection_counts(self, options: CallbackOptions) -> list[Observation]:
        pools = self.get_pools()
        self.record_durations(pools)
        values: list[tuple[dict[str, str], float]] = []
        for client, status in pools:
            attributes = PoolMetricInstruments.get_attributes(client, status)
            values.append(({**attributes, CONNECTION_STATE_ATTRIBUTE: IDLE_CONNECTION_STATE}, status.idle))
            values.append(({**attributes, CONNECTION_STATE_ATTRIBUTE: USED_CONNECTION_STATE}, status.in_use))
        return PoolMetricInstruments.get_summed_observations(values)

    def observe_max_sizes(self, options: CallbackOptions) -> list[Observation]:
        return self.observe(lambda status: status.max_size)

    def observe_min_sizes(self, options: CallbackOptions) -> list[Observation]:
        return self.observe(lambda status: status.min_size)

    def observe_pending_requests(self, options: CallbackOptions) -> list[Observation]:
        return self.observe(lambda status: status.waiting)

    def observe_timeouts(self, options: CallbackOptions) -> list[Observation]:
        return self.observe(lambda status: status.acquire_timeouts)

    def observe_connect_failures(self, options: CallbackOptions) -> list[Observation]:
        return self.observe(lambda status: status.connect_failures)

    def observe(self, get_value: Callable[[PoolStatus], float]) -> list[Observation]:
        """One value of every pool, summed by attributes.

        Args:
            get_value: Reads the value off a pool's status.

        Returns:
            The observations.
        """
        return PoolMetricInstruments.get_summed_observations(
            (PoolMetricInstruments.get_attributes(client, status), get_value(status))
            for client, status in self.get_pools()
        )

    def record_durations(self, pools: list[tuple[DatabaseClient, PoolStatus]]) -> None:
        """Records the waits and connects the pools measured since the previous collection.

        Args:
            pools: The pools.
        """
        with self.lock:
            for client, status in pools:
                attributes = PoolMetricInstruments.get_attributes(client, status)
                wait_cursor, connect_cursor = self.cursors.get(client, (0, 0))
                wait_cursor, waits, _ = client.pool_statistics.get_waits_since(wait_cursor)
                connect_cursor, connects, _ = client.pool_statistics.get_connects_since(connect_cursor)
                self.cursors[client] = (wait_cursor, connect_cursor)
                for seconds in waits:
                    self.wait_time.record(seconds, attributes)
                for seconds in connects:
                    self.create_time.record(seconds, attributes)
