"""What a pool of connections holds and has done (``DatabaseClient.get_pool_status()``,
``Connections.get_pool_statuses()``) on every driver: the connections taken and waited for, the waits
that ran out (``PoolTimeoutError``, ``PoolAcquireTimedOut``), the failures to connect
(``ConnectionFailed``), the role of each pool, and the counters kept across a reconnect."""

import asyncio
import contextlib
import os
import tempfile

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.dialects.base.client.constants import POOL_TIMEOUT_MESSAGE
from hare.exceptions import DBConnectionError, PoolTimeoutError, UnSupportedError
from hare.instrumentation import Observers
from hare.instrumentation.declarations import ConnectionFailed, PoolAcquireTimedOut
from hare.instrumentation.enums import PoolRole
from hare.instrumentation.pools.pool_metrics import PoolMetrics
from tests.utils.database_under_test import DatabaseUnderTest

#: The settings of a pool of one connection whose waits run out quickly.
SINGLE_CONNECTION_SETTINGS = {"min_size": 1, "max_size": 1, "pool_acquire_timeout": 0.3}


def is_postgres() -> bool:
    return DatabaseUnderTest.get_dialect().name == "postgresql"


@contextlib.asynccontextmanager
async def single_connection_client():
    """A client of one connection apart from the test's own - a pool of one connection on
    PostgreSQL, a database file of its own on SQLite, which has one connection anyway."""
    if is_postgres():
        settings = {**DatabaseUnderTest.get_direct_credentials({}), **SINGLE_CONNECTION_SETTINGS}
        client = Connections.current().create_independent("models", settings)
        try:
            yield client
        finally:
            await client.close()
        return
    with tempfile.TemporaryDirectory() as directory:
        client = Connections.current().create_independent(
            "models", {"file_path": os.path.join(directory, "pool.sqlite3")}
        )
        try:
            yield client
        finally:
            await client.close()


async def wait_for_waiting(client, count: int) -> None:
    for _ in range(200):
        status = client.get_pool_status()
        if status is not None and status.waiting >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {count} waiting: {client.get_pool_status()}")


@pytest.mark.asyncio
@requires_features(supports_pool_status=True)
async def test_the_connection_s_own_pool_is_reported(db):
    client = Connections.current().get_own("models")
    await client.execute_dicts("SELECT 1 AS one")
    status = client.get_pool_status()
    assert status is not None
    assert (status.connection_alias, status.role, status.schema) == ("models", PoolRole.OWN, None)
    assert status.size >= 1
    assert status.in_use == status.size - status.idle
    assert status.acquire_count >= 1
    assert status.connect_count >= 1
    assert status in Connections.get_pool_statuses("models")
    assert Connections.get_pool_statuses("no_such_connection") == []


@pytest.mark.asyncio
@requires_features(supports_pool_status=False)
async def test_a_client_reporting_no_pool_status_says_so(db):
    client = Connections.current().get_own("models")
    await client.execute_dicts("SELECT 1 AS one")
    with pytest.raises(UnSupportedError, match="reports no pool status"):
        client.get_pool_status()
    assert Connections.get_pool_statuses() == []


@pytest.mark.asyncio
@requires_features(supports_pool_status=True)
async def test_a_task_waiting_for_the_taken_connection_is_counted(db):
    async with single_connection_client() as client:
        await client.execute_dicts("SELECT 1 AS one")
        before = client.get_pool_status()
        async with client.acquire_connection():
            waiting_query = asyncio.create_task(client.execute_dicts("SELECT 1 AS one"))
            await wait_for_waiting(client, 1)
            status = client.get_pool_status()
            assert (status.in_use, status.idle, status.waiting) == (1, 0, 1)
        await waiting_query
        after = client.get_pool_status()
        assert after.waiting == 0
        assert after.acquire_count >= before.acquire_count + 2


@pytest.mark.asyncio
@requires_features(supports_pool_status=True)
async def test_the_waits_are_measured_only_while_the_pool_metrics_are_enabled(db):
    async with single_connection_client() as client:
        await client.execute_dicts("SELECT 1 AS one")
        cursor, _, _ = client.pool_statistics.get_waits_since(0)
        await client.execute_dicts("SELECT 1 AS one")
        assert client.pool_statistics.get_waits_since(cursor)[1] == []
        PoolMetrics.enable()
        try:
            async with client.acquire_connection():
                waiting_query = asyncio.create_task(client.execute_dicts("SELECT 1 AS one"))
                await wait_for_waiting(client, 1)
                await asyncio.sleep(0.05)
            await waiting_query
        finally:
            PoolMetrics.disable()
        new_cursor, waits, skipped = client.pool_statistics.get_waits_since(cursor)
        assert new_cursor > cursor
        assert skipped == 0
        assert max(waits) >= 0.04
        assert client.get_pool_status().acquire_wait_seconds_total >= 0.04
    assert PoolMetrics.enabled is False


@pytest.mark.asyncio
@requires_features(supports_pool_status=True)
async def test_a_wait_running_out_raises_one_error_counted_and_reported(db):
    if not is_postgres():
        pytest.skip("SQLite's one connection has no wait timeout")
    timed_out: list[PoolAcquireTimedOut] = []
    async with single_connection_client() as client:
        await client.execute_dicts("SELECT 1 AS one")
        with Observers.observing(PoolAcquireTimedOut, timed_out.append):
            async with client.acquire_connection():
                with pytest.raises(PoolTimeoutError) as error:
                    await client.execute_dicts("SELECT 1 AS one")
        assert error.value.args[0] == POOL_TIMEOUT_MESSAGE.format(seconds=0.3, connection_alias="models")
        assert isinstance(error.value, DBConnectionError)
        assert client.get_pool_status().acquire_timeouts == 1
    assert [(event.connection_alias, event.role, event.waited_seconds, event.max_size) for event in timed_out] == [
        ("models", PoolRole.INDEPENDENT, 0.3, 1)
    ]


@pytest.mark.asyncio
async def test_a_failure_to_connect_is_counted_and_reported_per_attempt(db):
    failed: list[ConnectionFailed] = []
    if is_postgres():
        client = Connections.current().create_independent(
            "models", {"port": 1, "connect_max_retries": 1, "connect_retry_backoff_base_seconds": 0.01}
        )
        expected_attempts = [(1, True), (2, False)]
    else:
        missing_directory = os.path.join(tempfile.gettempdir(), "hare_no_such_directory", "missing.sqlite3")
        client = Connections.current().create_independent("models", {"file_path": missing_directory})
        expected_attempts = [(1, False)]
    try:
        with Observers.observing(ConnectionFailed, failed.append), pytest.raises(DBConnectionError):
            await client.execute_dicts("SELECT 1 AS one")
    finally:
        await client.close()
    assert client.pool_statistics.get_counts()[4] == len(expected_attempts)
    assert [(event.attempt, event.will_retry) for event in failed] == expected_attempts
    assert {(event.connection_alias, event.role) for event in failed} == {("models", PoolRole.INDEPENDENT)}
    assert all(event.error_type and event.address for event in failed)


@pytest.mark.asyncio
@requires_features(supports_pool_status=True)
async def test_the_counters_outlive_a_reconnect(db):
    # A reconnect (Connections.reconnect()) closes each client and the next query opens a new pool on
    # the same client - the test's own client sits in its transaction, so one apart does it.
    async with single_connection_client() as client:
        await client.execute_dicts("SELECT 1 AS one")
        before = client.get_pool_status()
        await client.close()
        assert client.get_pool_status() is None
        await client.execute_dicts("SELECT 1 AS one")
        after = client.get_pool_status()
        assert after.acquire_count > before.acquire_count
        assert after.connect_count > before.connect_count


@pytest.mark.asyncio
@requires_features(supports_pool_status=True)
async def test_a_pool_is_listed_while_open_with_the_role_its_maker_gave(db):
    own = Connections.current().get_own("models")
    independent = own.create_independent_client(SINGLE_CONNECTION_SETTINGS if is_postgres() else {})
    assert independent.pool_role is PoolRole.INDEPENDENT
    try:
        await independent.execute_dicts("SELECT 1 AS one")
        roles = {status.role for status in Connections.get_pool_statuses("models")}
        assert PoolRole.INDEPENDENT in roles
    finally:
        await independent.close()
    assert independent.get_pool_status() is None


@requires_features(supports_pool_status=True)
def test_a_tenant_schema_s_client_has_its_role(db):
    if not is_postgres():
        pytest.skip("SQLite has no schema per tenant")
    client = Connections.current().create_independent("models", {"tenant_schema_template": "tenant_{tenant}"})
    assert client.get_schema_client("tenant_one").pool_role is PoolRole.TENANT_SCHEMA
