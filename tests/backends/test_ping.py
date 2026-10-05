"""DatabaseClient.ping() - a generic health-check built on the existing execute(), no
per-backend override needed since sqlite and every Postgres driver both understand SELECT 1."""

import asyncio
import inspect
import time

import pytest

from hare import Connections
from hare.dialects.base.client import DatabaseClient
from hare.dialects.base.client.constants import PING_TIMEOUT_SECONDS
from hare.exceptions import DBConnectionError, OperationalError


@pytest.mark.asyncio
async def test_ping_returns_true_on_live_connection(db_simple):
    assert await Connections.get("models").ping() is True


@pytest.mark.asyncio
async def test_ping_returns_false_on_operational_error(db_simple, monkeypatch):
    conn = Connections.get("models")

    async def raise_operational_error(*args, **kwargs):
        raise OperationalError("simulated failure")

    monkeypatch.setattr(conn, "execute", raise_operational_error)

    assert await conn.ping() is False


@pytest.mark.asyncio
async def test_ping_returns_false_on_db_connection_error(db_simple, monkeypatch):
    conn = Connections.get("models")

    async def raise_connection_error(*args, **kwargs):
        raise DBConnectionError("simulated connection loss")

    monkeypatch.setattr(conn, "execute", raise_connection_error)

    assert await conn.ping() is False


@pytest.mark.asyncio
async def test_ping_does_not_swallow_unrelated_exceptions(db_simple, monkeypatch):
    """Only DBConnectionError/OperationalError mean "unhealthy" - anything else (e.g. a genuine
    programming error) should still propagate rather than being silently reported as False."""
    conn = Connections.get("models")

    async def raise_value_error(*args, **kwargs):
        raise ValueError("not a connection health issue")

    monkeypatch.setattr(conn, "execute", raise_value_error)

    with pytest.raises(ValueError):
        await conn.ping()


@pytest.mark.asyncio
async def test_ping_returns_false_when_the_server_never_answers(db_simple, monkeypatch):
    """A frozen server (network partition, hung process) never makes a driver raise - the call
    just waits forever. ping() must bound itself and report unhealthy instead of hanging."""
    conn = Connections.get("models")

    async def hang_forever(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(conn, "execute", hang_forever)

    start = time.monotonic()
    # The outer wait_for only keeps a regression from hanging the suite forever.
    assert await asyncio.wait_for(conn.ping(timeout=0.2), 5) is False
    assert time.monotonic() - start < 2.0


@pytest.mark.asyncio
async def test_ping_returns_false_on_driver_level_timeout(db_simple, monkeypatch):
    """A driver-native command timeout surfaces as TimeoutError - that is "unhealthy" too."""
    conn = Connections.get("models")

    async def raise_timeout(*args, **kwargs):
        raise TimeoutError("driver command timeout")

    monkeypatch.setattr(conn, "execute", raise_timeout)

    assert await conn.ping() is False


def test_ping_has_its_own_short_default_timeout():
    """Independent of any connection-level command_timeout (off by default)."""
    default_timeout = inspect.signature(DatabaseClient.ping).parameters["timeout"].default
    assert default_timeout == PING_TIMEOUT_SECONDS == 5.0


@pytest.mark.asyncio
async def test_ping_does_not_report_a_slow_but_successful_query_as_unhealthy(db_simple, monkeypatch):
    conn = Connections.get("models")

    async def slow_query(*args, **kwargs):
        await asyncio.sleep(0.1)
        return 1, [{"?column?": 1}]

    monkeypatch.setattr(conn, "execute", slow_query)

    assert await conn.ping(timeout=2.0) is True
