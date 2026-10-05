"""``HealthCheck.run()`` on live connections: a reachable one is healthy with its ping's latency and
pools, an unreachable one unhealthy without raising, the criteria of a connection of its own apply,
"since the last check" counts what happened between two runs, a run without pinging opens no pool,
and a check naming a connection the configuration lacks is refused."""

import asyncio
import os
import tempfile

import pytest

from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from hare.health import ConnectionCriteria, HealthCheck, HealthStatus
from hare.health.criteria import AcquireTimeouts, PingFailed, PoolSaturation
from hare.instrumentation.enums import PoolRole


def make_config(directory: str) -> dict:
    return {
        "connections": {
            "reachable": f"sqlite+aiosqlite://{os.path.join(directory, 'reachable.sqlite3')}",
            "unreachable": f"sqlite+aiosqlite://{os.path.join(directory, 'no_such_directory', 'unreachable.sqlite3')}",
        },
        "apps": {"health": {"models": ["tests.health.health_models"], "default_connection": "reachable"}},
    }


@pytest.mark.asyncio
async def test_a_reachable_connection_is_healthy_and_an_unreachable_one_unhealthy():
    with tempfile.TemporaryDirectory() as directory:
        async with HareContext() as context:
            await context.init(config=make_config(directory))
            report = await HealthCheck(timeout_seconds=5).run()
            reachable = report.connections["reachable"]
            unreachable = report.connections["unreachable"]
            assert report.status is HealthStatus.UNHEALTHY
            assert reachable.status is HealthStatus.HEALTHY
            assert reachable.latency_ms is not None and reachable.latency_ms > 0
            assert reachable.reasons == ()
            assert [(pool.role, pool.size) for pool in reachable.pools] == [(PoolRole.OWN, 1)]
            assert unreachable.status is HealthStatus.UNHEALTHY
            assert unreachable.latency_ms is None
            assert unreachable.error_type == "DBConnectionError"
            assert unreachable.reasons == ("the ping failed: DBConnectionError",)
            assert report.to_dict()["connections"]["unreachable"] == {"status": "unhealthy"}
            await context.close_connections()


@pytest.mark.asyncio
async def test_a_connection_of_its_own_criteria_and_the_listed_connections():
    with tempfile.TemporaryDirectory() as directory:
        async with HareContext() as context:
            await context.init(config=make_config(directory))
            health_check = HealthCheck(
                connections=["reachable"],
                criteria_by_connection={"reachable": ConnectionCriteria(degraded_when=[PoolSaturation(ratio=1.0)])},
            )
            report = await health_check.run()
            assert list(report.connections) == ["reachable"]
            # The ping's connection was given back before the pools were read.
            assert report.connections["reachable"].status is HealthStatus.HEALTHY
            client = context.connections.get_own("reachable")
            async with client.acquire_connection():
                busy = await HealthCheck(connections=["reachable"], degraded_when=[PoolSaturation(ratio=1.0)]).run(
                    ping=False
                )
            assert busy.connections["reachable"].status is HealthStatus.DEGRADED
            assert busy.connections["reachable"].reasons == (
                "1 of 1 connections of the own pool in use (at least 100%)",
            )
            await context.close_connections()


@pytest.mark.asyncio
async def test_a_run_without_pinging_opens_no_pool_and_the_ping_failure_is_no_criterion_then():
    with tempfile.TemporaryDirectory() as directory:
        async with HareContext() as context:
            await context.init(config=make_config(directory))
            report = await HealthCheck().run(ping=False)
            assert report.status is HealthStatus.HEALTHY
            assert all(
                connection.pools == () and connection.latency_ms is None for connection in report.connections.values()
            )
            assert context.connections.get_own("reachable").get_pool_status() is None
            await context.close_connections()


@pytest.mark.asyncio
async def test_since_the_last_check_counts_what_happened_between_two_runs():
    with tempfile.TemporaryDirectory() as directory:
        async with HareContext() as context:
            await context.init(config=make_config(directory))
            client = context.connections.get_own("reachable")
            await client.execute_dicts("SELECT 1 AS one")
            health_check = HealthCheck(connections=["reachable"], degraded_when=[AcquireTimeouts(at_least=1)])
            # A timeout counted before the first run is in the first run's change, not in the second's.
            client.pool_statistics.add_timeout()
            first = await health_check.run()
            second = await health_check.run()
            assert first.connections["reachable"].status is HealthStatus.DEGRADED
            assert second.connections["reachable"].status is HealthStatus.HEALTHY
            await context.close_connections()


@pytest.mark.asyncio
async def test_waiting_tasks_degrade_a_connection_by_default():
    with tempfile.TemporaryDirectory() as directory:
        async with HareContext() as context:
            await context.init(config=make_config(directory))
            client = context.connections.get_own("reachable")
            await client.execute_dicts("SELECT 1 AS one")
            async with client.acquire_connection():
                waiting_query = asyncio.create_task(client.execute_dicts("SELECT 1 AS one"))
                for _ in range(200):
                    if client.get_pool_status().waiting:
                        break
                    await asyncio.sleep(0.01)
                report = await HealthCheck(connections=["reachable"], unhealthy_when=[]).run(ping=False)
            await waiting_query
            reasons = report.connections["reachable"].reasons
            assert report.connections["reachable"].status is HealthStatus.DEGRADED
            assert "1 tasks wait for a connection of the own pool (at least 1)" in reasons
            await context.close_connections()


@pytest.mark.asyncio
async def test_a_check_naming_a_connection_the_configuration_lacks_is_refused():
    with tempfile.TemporaryDirectory() as directory:
        async with HareContext() as context:
            await context.init(config=make_config(directory))
            for health_check in (
                HealthCheck(connections=["reachable", "nowhere"]),
                HealthCheck(criteria_by_connection={"nowhere": ConnectionCriteria(unhealthy_when=[PingFailed()])}),
            ):
                with pytest.raises(ConfigurationError, match=r"lacks: \['nowhere'\]"):
                    await health_check.run()
            await context.close_connections()


@pytest.mark.asyncio
async def test_the_database_under_test_is_healthy(db):
    report = await HealthCheck(connections=["models"], degraded_when=[]).run()
    models = report.connections["models"]
    assert models.status is HealthStatus.HEALTHY, models.reasons
    assert models.latency_ms is not None


@pytest.mark.asyncio
async def test_the_server_past_a_transaction_pooler_is_pinged_too(db):
    # Through PgBouncer the server itself (direct_host) is pinged as well; straight to the server
    # there is no second ping.
    report = await HealthCheck(connections=["models"], degraded_when=[], check_direct=True).run()
    assert report.connections["models"].status is HealthStatus.HEALTHY, report.connections["models"].reasons
