from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from operator import attrgetter
from typing import TYPE_CHECKING, ClassVar
from weakref import ReferenceType, WeakSet, ref

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
from hare.core.log import logger
from hare.instrumentation.pools.declarations import PoolMeasurement
from hare.instrumentation.pools.enums import PoolMetricEvent
from hare.instrumentation.pools.messages import POOL_RECORDER_ERROR_MESSAGE
from hare.instrumentation.pools.pool_metrics import PoolMetrics
from hare.instrumentation.pools.pool_metrics_recorder import PoolMetricsRecorder
from hare.instrumentation.pools.pool_registry import PoolRegistry

if TYPE_CHECKING:
    from opentelemetry.metrics import MeterProvider

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.declarations import PoolStatus


class PoolMetricInstruments:
    """The OpenTelemetry metrics of the pools of connections - every open pool of the process
    (``PoolRegistry``), read when the metrics are collected, on the exporter's thread. Pools with
    the same attributes (one connection of several ``HareContext`` objects) are summed.

    Completed measurements feed counters and histograms directly. Gauge exports only read
    current occupancy; slow exporters and closing a pool cannot subtract accumulated events.
    Active collectors share one subscription. The global provider can start as a forwarding
    provider and take on the identity of the SDK provider installed later.

    Args:
        meter_provider: The provider that owns these instruments.
    """

    registered_instruments: ClassVar[WeakSet[PoolMetricInstruments]] = WeakSet()
    providers_lock: ClassVar[threading.RLock] = threading.RLock()
    default_instruments: ClassVar[ReferenceType[PoolMetricInstruments] | None] = None
    active_instruments: ClassVar[tuple[PoolMetricInstruments, ...]] = ()

    @classmethod
    def for_provider(cls, meter_provider: MeterProvider | None) -> PoolMetricInstruments:
        """Get the one set of instruments owned by the explicit or current global provider."""
        default_provider = metrics.get_meter_provider()
        provider = default_provider if meter_provider is None else meter_provider
        with cls.providers_lock:
            instruments = next(
                (entry for entry in cls.registered_instruments if entry.meter_provider is provider), None
            )
            default = cls.get_default_instruments() if provider is default_provider else None
            if instruments is None:
                if default is not None:
                    return default
                instruments = cls(provider)
                cls.registered_instruments.add(instruments)
            if provider is default_provider and default is None:
                cls.default_instruments = ref(instruments)
            return instruments

    @classmethod
    def get_default_instruments(cls) -> PoolMetricInstruments | None:
        """The collector registered through the current or forwarding global provider."""
        reference = cls.default_instruments
        return None if reference is None else reference()

    def __init__(self, meter_provider: MeterProvider) -> None:
        self.meter_provider = meter_provider
        meter = metrics.get_meter(__name__, meter_provider=self.meter_provider)
        self.active_users: set[object] = set()
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
        self.timeouts = meter.create_counter(CONNECTION_TIMEOUTS_METRIC, unit=TIMEOUT_UNIT)
        self.connect_failures = meter.create_counter(CONNECT_FAILURES_METRIC, unit=FAILURE_UNIT)
        self.wait_time = meter.create_histogram(CONNECTION_WAIT_TIME_METRIC, unit=SECONDS_UNIT)
        self.create_time = meter.create_histogram(CONNECTION_CREATE_TIME_METRIC, unit=SECONDS_UNIT)

    def start(self, token: object) -> None:
        """Activate this collector and subscribe when the first collector starts."""
        cls = type(self)
        with cls.providers_lock:
            if self.active_users:
                self.active_users.add(token)
                return
            self.active_users.add(token)
            previous = cls.active_instruments
            cls.active_instruments = (*previous, self)
            if not previous:
                PoolMetrics.enable()
                PoolMetricsRecorder.subscribe(cls.record)

    def stop(self, token: object) -> None:
        """Deactivate the collector after its last user and release the shared subscription."""
        cls = type(self)
        with cls.providers_lock:
            had_users = bool(self.active_users)
            self.active_users.discard(token)
            if not had_users or self.active_users:
                return
            cls.active_instruments = tuple(
                instruments for instruments in cls.active_instruments if instruments is not self
            )
            if not cls.active_instruments:
                PoolMetricsRecorder.unsubscribe(cls.record)
                PoolMetrics.disable()

    @classmethod
    def record(cls, measurement: PoolMeasurement) -> None:
        """Send an event once to each effective provider, including the resolved global SDK."""
        default = cls.get_default_instruments()
        default_provider = metrics.get_meter_provider() if default is not None else None
        reported: set[int] = set()
        for instruments in cls.active_instruments:
            provider = default_provider if instruments is default else instruments.meter_provider
            provider_id = id(provider)
            if provider_id in reported:
                continue
            reported.add(provider_id)
            try:
                instruments.record_measurement(measurement)
            except Exception:
                logger.exception(POOL_RECORDER_ERROR_MESSAGE)

    def record_measurement(self, measurement: PoolMeasurement) -> None:
        """Record an event on its completion thread, using labels captured by the client."""
        attributes = {
            CONNECTION_POOL_NAME_ATTRIBUTE: measurement.connection_alias,
            POOL_ROLE_ATTRIBUTE: measurement.role,
            DB_SYSTEM_NAME_ATTRIBUTE: measurement.system,
        }
        if measurement.schema is not None:
            attributes[POOL_SCHEMA_ATTRIBUTE] = measurement.schema
        if measurement.event is PoolMetricEvent.WAIT:
            self.wait_time.record(measurement.value, attributes)
        elif measurement.event is PoolMetricEvent.CONNECT:
            self.create_time.record(measurement.value, attributes)
        self.timeouts.add(measurement.value if measurement.event is PoolMetricEvent.TIMEOUT else 0, attributes)
        self.connect_failures.add(
            measurement.value if measurement.event is PoolMetricEvent.CONNECT_FAILURE else 0, attributes
        )

    def is_provider_active(self) -> bool:
        """A gauge stays active while either collector for its effective provider runs."""
        cls = type(self)
        active = cls.active_instruments
        if self in active:
            return True
        default = cls.get_default_instruments()
        if not active or default is None:
            return False
        provider = metrics.get_meter_provider()
        if self is default:
            return any(instruments.meter_provider is provider for instruments in active)
        return self.meter_provider is provider and default in active

    def get_pools(self) -> list[tuple[DatabaseClient, PoolStatus]]:
        """Every open pool reporting its status.

        Returns:
            Each pool's client and status - none while stopped.
        """
        if not self.is_provider_active():
            return []
        return list(PoolRegistry.iter_statuses())

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
        """The idle and in-use connections of every pool, observed by the gauge callback.

        Args:
            options: The callback's options.

        Returns:
            The observations.
        """
        pools = self.get_pools()
        values: list[tuple[dict[str, str], float]] = []
        for client, status in pools:
            attributes = PoolMetricInstruments.get_attributes(client, status)
            values.append(({**attributes, CONNECTION_STATE_ATTRIBUTE: IDLE_CONNECTION_STATE}, status.idle))
            values.append(({**attributes, CONNECTION_STATE_ATTRIBUTE: USED_CONNECTION_STATE}, status.in_use))
        return PoolMetricInstruments.get_summed_observations(values)

    def observe_max_sizes(self, options: CallbackOptions) -> list[Observation]:
        return self.observe(attrgetter("max_size"))

    def observe_min_sizes(self, options: CallbackOptions) -> list[Observation]:
        return self.observe(attrgetter("min_size"))

    def observe_pending_requests(self, options: CallbackOptions) -> list[Observation]:
        return self.observe(attrgetter("waiting"))

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
