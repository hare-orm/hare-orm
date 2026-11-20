"""The OpenTelemetry metrics of the pools of connections: every open pool's connections by state, its
bounds, its waiting tasks, timeouts and failures to connect, the waits and connects as histograms
recorded at completion independently of collection; pools with equal attributes summed; read from
another thread; nothing once uninstrumented."""

import asyncio
import os
import subprocess  # nosec B404 - the test runs its own Python program.
import sys
import tempfile

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.metrics.view import DropAggregation, View

from hare import Connections
from hare.contrib.opentelemetry import OpenTelemetryInstrumentor
from hare.contrib.test import requires_features
from hare.dialects.base.client.pool.pool_statistics import PoolStatistics
from hare.instrumentation.pools.declarations import PoolMeasurement
from hare.instrumentation.pools.enums import PoolMetricEvent
from hare.instrumentation.pools.pool_metrics import PoolMetrics
from hare.instrumentation.pools.pool_metrics_recorder import PoolMetricsRecorder
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.database_independent
@pytest.mark.parametrize("explicit_first", [False, True], ids=["global-first", "explicit-first"])
def test_late_global_provider_shares_pool_metrics_with_its_explicit_user(explicit_first):
    probe = """
import asyncio
import sys
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from hare.contrib.opentelemetry import OpenTelemetryInstrumentor
from hare.core.hare_context import HareContext

def points(reader, name):
    return [
        point for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics for metric in scope.metrics
        if metric.name == name for point in metric.data.data_points
    ]

async def run():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    explicit = OpenTelemetryInstrumentor(meter_provider=provider, query_spans=False)
    implicit = OpenTelemetryInstrumentor(query_spans=False)
    first, second = (explicit, implicit) if sys.argv[1] == "True" else (implicit, explicit)
    first.instrument()
    if first is explicit:
        second.instrument()
        metrics.set_meter_provider(provider)
    else:
        metrics.set_meter_provider(provider)
        second.instrument()
    try:
        async with HareContext() as context:
            await context.init(config={"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {}})
            client = context.connections.get_main("default")
            await client.execute_dicts("SELECT 1 AS one")
            assert client.get_pool_status().connect_count == 1
            assert sum(point.count for point in points(reader, "db.client.connection.create_time")) == 1
            first.uninstrument()
            assert sum(point.value for point in points(reader, "db.client.connection.count")) == 1
            second.uninstrument()
            assert not points(reader, "db.client.connection.count")
            await context.close_connections()
    finally:
        first.uninstrument()
        second.uninstrument()
        provider.shutdown()

asyncio.run(run())
"""
    result = subprocess.run(  # nosec B603 - the interpreter and the entire program come from this test.
        [sys.executable, "-c", probe, str(explicit_first)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


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
    instruments = [OpenTelemetryInstrumentor(meter_provider=provider, query_spans=False) for provider in providers]
    statistics = PoolStatistics(capacity=2)
    statistics.set_recorder(
        lambda event, value: PoolMetricsRecorder.record(
            PoolMeasurement("bounded", "main", None, "sqlite", PoolMetricEvent(event), value)
        )
    )
    try:
        for instrument in instruments:
            instrument.instrument()
            instrument.instrument()
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
        instruments[0].uninstrument()
        statistics.add_wait(0.25)
        instruments[0].instrument()
        statistics.add_wait(0.25)
        assert get_points(readers[0].get_metrics_data(), "db.client.connection.wait_time")[0].count == 101
        assert get_points(readers[1].get_metrics_data(), "db.client.connection.wait_time")[0].count == 102
    finally:
        statistics.set_recorder(None)
        for instrument in instruments:
            instrument.uninstrument()
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
@pytest.mark.parametrize("first_state", ["inactive", "stopped", "active"])
async def test_instrumentors_sharing_a_meter_provider_keep_one_set_of_pool_metrics(db_simple, first_state):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    first = OpenTelemetryInstrumentor(meter_provider=provider, query_spans=False)
    second = OpenTelemetryInstrumentor(meter_provider=provider, query_spans=False)
    client, directory = make_independent_client()
    attributes = {"db.client.connection.pool.name": "models", "hare.pool.role": "independent"}
    try:
        if first_state != "inactive":
            first.instrument()
        if first_state == "stopped":
            first.uninstrument()
        second.instrument()
        assert await client.execute_dicts("SELECT 1 AS one") == [{"one": 1}]
        (created,) = get_points(reader.get_metrics_data(), "db.client.connection.create_time", **attributes)
        assert created.count == client.get_pool_status().connect_count == 1

        first.uninstrument()
        report = reader.get_metrics_data()
        assert get_points(report, "db.client.connection.count", **attributes)
        assert get_points(report, "db.client.connection.max", **attributes)
        assert get_points(report, "db.client.connection.idle.min", **attributes)
        assert get_points(report, "db.client.connection.pending_requests", **attributes)

        second.uninstrument()
        assert get_points(reader.get_metrics_data(), "db.client.connection.max", **attributes) == []
    finally:
        first.uninstrument()
        second.uninstrument()
        await client.close()
        if directory is not None:
            directory.cleanup()
        provider.shutdown()


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
