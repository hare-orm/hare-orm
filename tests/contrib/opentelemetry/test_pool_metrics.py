"""The OpenTelemetry metrics of the pools of connections: every open pool's connections by state, its
bounds, its waiting tasks, timeouts and failures to connect, the waits and connects as histograms
recorded at completion independently of collection; pools with equal attributes summed; read from
another thread; nothing once uninstrumented."""

import asyncio
import os
import tempfile

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.metrics.view import DropAggregation, View

from hare import Connections
from hare.contrib.opentelemetry import OpenTelemetryInstrumentor
from hare.contrib.opentelemetry.pool_metric_instruments import PoolMetricInstruments
from hare.contrib.test import requires_features
from hare.dialects.base.client.pool.pool_statistics import PoolStatistics
from hare.instrumentation.pools.declarations import PoolMeasurement
from hare.instrumentation.pools.enums import PoolMetricEvent
from hare.instrumentation.pools.pool_metrics import PoolMetrics
from hare.instrumentation.pools.pool_metrics_recorder import PoolMetricsRecorder
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.database_independent
def test_histogram_only_exports_keep_all_events_across_restart():
    readers = [InMemoryMetricReader(), InMemoryMetricReader()]
    providers = [
        MeterProvider(
            metric_readers=[reader],
            views=[
                View(instrument_name="*", aggregation=DropAggregation()),
                View(instrument_name="db.client.connection.wait_time"),
            ],
        )
        for reader in readers
    ]
    instruments = [PoolMetricInstruments(provider) for provider in providers]
    statistics = PoolStatistics(capacity=2)
    statistics.set_recorder(
        lambda event, value: PoolMetricsRecorder.record(
            PoolMeasurement("bounded", "main", None, "sqlite", PoolMetricEvent(event), value)
        )
    )
    try:
        for instrument in instruments:
            instrument.start()
            instrument.start()
        for _ in range(100):
            statistics.add_wait(0.25)
        for reader in readers:
            points = get_points(
                reader.get_metrics_data(),
                "db.client.connection.wait_time",
                **{"db.client.connection.pool.name": "bounded"},
            )
            assert (points[0].count, points[0].sum) == (100, 25)
        assert statistics.get_waits_since(0)[2] == 98
        instruments[0].stop()
        statistics.add_wait(0.25)
        instruments[0].start()
        statistics.add_wait(0.25)
        assert get_points(readers[0].get_metrics_data(), "db.client.connection.wait_time")[0].count == 101
        assert get_points(readers[1].get_metrics_data(), "db.client.connection.wait_time")[0].count == 102
    finally:
        statistics.set_recorder(None)
        for instrument in instruments:
            instrument.stop()
        for provider in providers:
            provider.shutdown()


@pytest.fixture
def metric_reader():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    instrumentor = OpenTelemetryInstrumentor(meter_provider=provider, query_spans=False)
    instrumentor.instrument()
    yield reader
    instrumentor.uninstrument()
    provider.shutdown()


def get_points(data, metric_name, **attributes):
    """The data points of a metric of one collection whose attributes hold the given ones."""
    points = []
    for resource_metrics in data.resource_metrics if data else ():
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                points += [
                    point
                    for point in metric.data.data_points
                    if all(point.attributes.get(name) == value for name, value in attributes.items())
                ]
    return points


def make_independent_client():
    """A client of the connection apart from the test's own - a pool of its own."""
    if DatabaseUnderTest.get_dialect().name == "postgresql":
        settings = {**DatabaseUnderTest.get_direct_credentials({}), "min_size": 1, "max_size": 2}
        return Connections.current().create_independent("models", settings), None
    directory = tempfile.TemporaryDirectory()
    client = Connections.current().create_independent(
        "models", {"file_path": os.path.join(directory.name, "m.sqlite3")}
    )
    return client, directory


@pytest.mark.asyncio
@requires_features(supports_pool_status=True)
async def test_every_open_pool_is_reported_by_its_attributes(db_simple, metric_reader):
    assert PoolMetrics.enabled is True
    await Connections.current().get_main("models").execute_dicts("SELECT 1 AS one")
    pool_attributes = {"db.client.connection.pool.name": "models", "hare.pool.role": "main"}
    data = metric_reader.get_metrics_data()
    idle = get_points(data, "db.client.connection.count", **pool_attributes, **{"db.client.connection.state": "idle"})
    used = get_points(data, "db.client.connection.count", **pool_attributes, **{"db.client.connection.state": "used"})
    assert len(idle) == len(used) == 1
    status = Connections.current().get_main("models").get_pool_status()
    assert idle[0].value + used[0].value == status.size
    assert idle[0].attributes["db.system.name"] == Connections.current().get_main("models").dialect.otel_system_name
    (max_point,) = get_points(data, "db.client.connection.max", **pool_attributes)
    assert max_point.value == status.max_size
    (pending,) = get_points(data, "db.client.connection.pending_requests", **pool_attributes)
    assert pending.value == 0
    assert get_points(data, "db.client.connection.timeouts", **pool_attributes)[0].value == status.acquire_timeouts
    assert get_points(data, "hare.pool.connect_failures", **pool_attributes)[0].value == 0


@pytest.mark.asyncio
@requires_features(supports_pool_status=True)
async def test_pools_of_equal_attributes_are_summed_and_events_survive_closure(db, metric_reader):
    first, first_directory = make_independent_client()
    second, second_directory = make_independent_client()
    try:
        await first.execute_dicts("SELECT 1 AS one")
        await second.execute_dicts("SELECT 1 AS one")
        independent = {"db.client.connection.pool.name": "models", "hare.pool.role": "independent"}
        # Collection can read completed measurements from a different thread.
        data = await asyncio.to_thread(metric_reader.get_metrics_data)
        (max_point,) = get_points(data, "db.client.connection.max", **independent)
        assert max_point.value == first.get_pool_status().max_size + second.get_pool_status().max_size
        (wait_time,) = get_points(data, "db.client.connection.wait_time", **independent)
        (create_time,) = get_points(data, "db.client.connection.create_time", **independent)
        assert wait_time.count >= 2
        assert create_time.count >= 2
        # Older SDKs export a cumulative histogram only when new measurements arrived.
        # Leave one uncollected event to verify its history after both pools disappear.
        await first.execute_dicts("SELECT 1 AS one")
    finally:
        await first.close()
        await second.close()
        for directory in (first_directory, second_directory):
            if directory is not None:
                directory.cleanup()
    data = metric_reader.get_metrics_data()
    assert get_points(data, "db.client.connection.max", **independent) == []
    assert get_points(data, "db.client.connection.wait_time", **independent)[0].count == wait_time.count + 1


@pytest.mark.asyncio
@requires_features(supports_pool_status=True)
async def test_nothing_is_reported_once_uninstrumented(db_simple):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    instrumentor = OpenTelemetryInstrumentor(meter_provider=provider, query_spans=False)
    instrumentor.instrument()
    try:
        await Connections.current().get_main("models").execute_dicts("SELECT 1 AS one")
        assert get_points(reader.get_metrics_data(), "db.client.connection.max")
        instrumentor.uninstrument()
        assert PoolMetrics.enabled is False
        assert get_points(reader.get_metrics_data(), "db.client.connection.max") == []
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
