"""The health criteria and the report, on what a health check saw - no database: each criterion holds
or not on the counts it reads, refuses arguments of the wrong type or range, and the report gives
its details only when asked."""

from datetime import UTC, datetime

import pytest

from hare.dialects.base.client.declarations import PingResult
from hare.exceptions import ConfigurationError
from hare.health import ConnectionCheck, ConnectionCriteria, ConnectionHealth, HealthCheck, HealthReport, HealthStatus
from hare.health.criteria import (
    AcquireTimeouts,
    AcquireWaitTime,
    AllOf,
    ConnectFailures,
    PingFailed,
    PingLatency,
    PoolSaturation,
    WaitingRequests,
)
from hare.health.declarations import PoolChange
from hare.instrumentation.declarations import PoolStatus
from hare.instrumentation.enums import PoolRole
from hare.instrumentation.pools.pool_metrics import PoolMetrics


def make_pool(
    *,
    in_use=0,
    waiting=0,
    max_size=10,
    schema=None,
    role=PoolRole.OWN,
    taken=0,
    timeouts=0,
    connect_failures=0,
    waits=(),
):
    status = PoolStatus(
        "models", role, schema, max_size, max_size - in_use, in_use, waiting, 1, max_size, 0, 0, 0.0, 1, 0
    )
    return PoolChange(status, taken, timeouts, connect_failures, tuple(waits))


def make_check(*, ping=PingResult(True, 1.5, None), server_ping=None, pools=()):
    return ConnectionCheck("models", ping, server_ping, tuple(pools))


@pytest.mark.parametrize(
    ("criterion", "holding_check", "passing_check", "reason"),
    [
        (
            PingFailed(),
            make_check(ping=PingResult(False, None, "DBConnectionError")),
            make_check(),
            "the ping failed: DBConnectionError",
        ),
        (
            PingFailed(),
            make_check(server_ping=PingResult(False, None, "TimeoutError")),
            make_check(ping=None),
            "the ping of the server past the pooler failed: TimeoutError",
        ),
        (
            PingLatency(max_ms=100),
            make_check(ping=PingResult(True, 150.0, None)),
            make_check(ping=PingResult(True, 100.0, None)),
            "the ping took 150.0 ms (over 100 ms)",
        ),
        (
            PoolSaturation(ratio=0.9),
            make_check(pools=[make_pool(in_use=9)]),
            make_check(pools=[make_pool(in_use=8)]),
            "9 of 10 connections of the own pool in use (at least 90%)",
        ),
        (
            WaitingRequests(at_least=2),
            make_check(pools=[make_pool(waiting=2, schema="tenant_one", role=PoolRole.TENANT_SCHEMA)]),
            make_check(pools=[make_pool(waiting=1)]),
            "2 tasks wait for a connection of the pool of tenant schema tenant_one (at least 2)",
        ),
        (
            AcquireTimeouts(at_least=3),
            make_check(pools=[make_pool(timeouts=3)]),
            make_check(pools=[make_pool(timeouts=2)]),
            "3 waits for a connection of the own pool ran out since the last check (at least 3)",
        ),
        (
            ConnectFailures(at_least=1),
            make_check(pools=[make_pool(connect_failures=1, role=PoolRole.DIRECT)]),
            make_check(pools=[make_pool()]),
            "1 connects of the direct pool failed since the last check (at least 1)",
        ),
        (
            AllOf(PoolSaturation(ratio=1.0), WaitingRequests(at_least=1)),
            make_check(pools=[make_pool(in_use=10, waiting=1)]),
            make_check(pools=[make_pool(in_use=10)]),
            "10 of 10 connections of the own pool in use (at least 100%); "
            "1 tasks wait for a connection of the own pool (at least 1)",
        ),
    ],
)
def test_a_criterion_holds_with_its_reason_and_passes_otherwise(criterion, holding_check, passing_check, reason):
    assert criterion.check(holding_check) == reason
    assert criterion.check(passing_check) is None


def test_the_longest_wait_since_the_last_check_is_judged():
    PoolMetrics.enable()
    try:
        criterion = AcquireWaitTime(max_seconds=0.5)
    finally:
        PoolMetrics.disable()
    assert criterion.requires_pool_metrics is True
    assert criterion.check(make_check(pools=[make_pool(waits=[0.1, 0.75])])) == (
        "a wait for a connection of the own pool took 0.750 s (over 0.5 s)"
    )
    assert criterion.check(make_check(pools=[make_pool(waits=[0.5])])) is None
    assert AllOf(criterion, PingFailed()).requires_pool_metrics is True


@pytest.mark.parametrize(
    ("make_criterion", "error_type", "message"),
    [
        (lambda: PoolSaturation(ratio=0), ValueError, "ratio must be above 0 and at most 1"),
        (lambda: PoolSaturation(ratio=1.5), ValueError, "ratio must be above 0 and at most 1"),
        (lambda: PoolSaturation(ratio="0.5"), TypeError, "ratio must be a number"),
        (lambda: PoolSaturation(ratio=float("nan")), ValueError, "ratio must be above 0"),
        (lambda: WaitingRequests(at_least=0), ValueError, "at_least must be at least 1"),
        (lambda: WaitingRequests(at_least=True), TypeError, "at_least must be an int"),
        (lambda: AcquireTimeouts(at_least=1.0), TypeError, "at_least must be an int"),
        (lambda: PingLatency(max_ms=60_001), ValueError, "max_ms must be above 0 and at most 60000"),
        (lambda: AcquireWaitTime(max_seconds=-1), ValueError, "max_seconds must be above 0"),
        (lambda: AllOf(PingFailed()), ValueError, "AllOf needs at least two criteria"),
        (lambda: AllOf(PingFailed(), "ping"), TypeError, "AllOf takes HealthCriterion objects"),
    ],
)
def test_a_criterion_refuses_an_argument_of_the_wrong_type_or_range(make_criterion, error_type, message):
    with pytest.raises(error_type, match=message):
        make_criterion()


@pytest.mark.parametrize(
    ("arguments", "error_type", "message"),
    [
        ({"timeout_seconds": 0}, ValueError, "timeout_seconds must be above 0 and at most 60"),
        ({"timeout_seconds": 61}, ValueError, "timeout_seconds must be above 0 and at most 60"),
        ({"degraded_when": PingFailed()}, TypeError, "degraded_when must be a sequence"),
        ({"unhealthy_when": ["ping"]}, TypeError, "unhealthy_when takes HealthCriterion objects"),
        ({"criteria_by_connection": {"models": [PingFailed()]}}, TypeError, "criteria_by_connection maps"),
        ({"criteria_by_connection": []}, TypeError, "criteria_by_connection must be a mapping"),
        ({"connections": "models"}, TypeError, "connections must be a sequence of connection names"),
        ({"check_direct": "yes"}, TypeError, "check_direct must be a bool"),
        ({"degraded_when": [AcquireWaitTime(max_seconds=1)]}, ConfigurationError, "call PoolMetrics.enable()"),
        (
            {
                "criteria_by_connection": {
                    "models": ConnectionCriteria(unhealthy_when=[AcquireWaitTime(max_seconds=1)])
                }
            },
            ConfigurationError,
            "call PoolMetrics.enable()",
        ),
    ],
)
def test_a_health_check_refuses_an_argument_of_the_wrong_type_or_range(arguments, error_type, message):
    with pytest.raises(error_type, match=message):
        HealthCheck(**arguments)


def test_a_report_is_as_bad_as_its_worst_connection_and_shows_details_only_when_asked():
    pool = make_pool(in_use=3).status
    report = HealthReport(
        datetime(2026, 10, 6, 14, 3, 11, tzinfo=UTC),
        {
            "default": ConnectionHealth(HealthStatus.DEGRADED, 1.8, ("busy",), None, (pool,)),
            "analytics": ConnectionHealth(HealthStatus.HEALTHY, 2.4, (), None, ()),
        },
    )
    assert report.status is HealthStatus.DEGRADED
    assert report.to_dict() == {
        "status": "degraded",
        "checked_at": "2026-10-06T14:03:11+00:00",
        "connections": {"default": {"status": "degraded"}, "analytics": {"status": "healthy"}},
    }
    details = report.to_dict(include_details=True)["connections"]["default"]
    assert details["latency_ms"] == 1.8
    assert details["reasons"] == ["busy"]
    assert details["pools"][0]["role"] == "own"
    assert details["pools"][0]["in_use"] == 3
    assert HealthReport(datetime.now(UTC), {}).status is HealthStatus.HEALTHY
    unhealthy = ConnectionHealth(HealthStatus.UNHEALTHY, None, ("x",), "OSError", ())
    assert HealthReport(datetime.now(UTC), {"a": unhealthy, "b": unhealthy}).status is HealthStatus.UNHEALTHY
