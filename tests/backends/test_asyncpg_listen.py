"""Tests for AsyncpgClient.listen() - LISTEN/NOTIFY on the asyncpg backend, mirroring
tests/backends/test_rust_pg_listen.py's coverage for the same public contract on rust_pg (the
default Postgres driver, which also implements listen())."""

import asyncio

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient
from hare.exceptions import ConfigurationError


def _asyncpg_client_or_skip() -> AsyncpgClient:
    client = Connections.get("models")
    if not isinstance(client, AsyncpgClient):
        pytest.skip("this test targets the asyncpg-specific listen() implementation, not rust_pg's own")
    return client


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_receives_notify_sent_from_a_separate_pooled_connection(db_simple):
    # db_simple, not db: the `db` fixture wraps Connections.get() in a per-test transaction client
    # for rollback-based isolation - .listen() needs the real client (its own connection
    # params live on the AsyncpgClient instance itself, never copied onto that lightweight
    # transactional wrapper).
    """NOTIFY works today via any pooled connection as plain SQL - execute() needs no
    changes for it. .listen() is the new piece: a dedicated connection whose callback actually
    fires when a DIFFERENT, ordinary pooled connection issues that NOTIFY."""
    client = _asyncpg_client_or_skip()

    received: list[tuple[int, str, str]] = []
    event = asyncio.Event()

    def callback(connection, pid, channel, payload):
        received.append((pid, channel, payload))
        event.set()

    listener_connection = await client.listen("hare_test_listen_channel", callback)
    try:
        await client.execute("NOTIFY hare_test_listen_channel, 'hello-from-notify'")
        await asyncio.wait_for(event.wait(), timeout=5)

        assert len(received) == 1
        pid, channel, payload = received[0]
        assert channel == "hare_test_listen_channel"
        assert payload == "hello-from-notify"
    finally:
        await listener_connection.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_with_wrong_password_raises_configuration_error_not_raw_exception(db_simple):
    """listen() opens its OWN dedicated connection via a direct asyncpg.connect() call, entirely
    bypassing create_connection()'s own exception mapping (added in an earlier round for exactly
    this scenario) - confirmed live that a wrong password leaked the raw
    asyncpg.exceptions.InvalidPasswordError instead of hare.exceptions.ConfigurationError."""
    _asyncpg_client_or_skip()
    shared = Connections.get("models")
    bad_client = type(shared)(
        connection_name="wrong_password_listen_test",
        user=shared.user,
        password=f"{shared.password}-definitely-wrong",
        database=shared.database,
        host=shared.host,
        port=shared.port,
    )
    with pytest.raises(ConfigurationError, match="Authentication failed"):
        await bad_client.listen("hare_test_listen_channel", lambda *args: None)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_with_nonexistent_database_raises_configuration_error(db_simple):
    """Same gap as the wrong-password case above, for the nonexistent-database branch of
    create_connection()'s own mapping - a database that doesn't exist is a configuration problem
    no retry fixes, like a wrong password."""
    _asyncpg_client_or_skip()
    shared = Connections.get("models")
    bad_client = type(shared)(
        connection_name="missing_database_listen_test",
        user=shared.user,
        password=shared.password,
        database="hare_orm_definitely_missing_database",
        host=shared.host,
        port=shared.port,
    )
    with pytest.raises(ConfigurationError, match="does not exist"):
        await bad_client.listen("hare_test_listen_channel", lambda *args: None)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_connection_survives_concurrent_pool_usage(db_simple):
    """The listener connection is deliberately never drawn from - or returned to - the
    connection pool: ordinary pooled queries running while it's still open must keep working
    normally, and the listener must keep receiving NOTIFYs throughout."""
    client = _asyncpg_client_or_skip()

    received: list[str] = []
    event = asyncio.Event()

    def callback(connection, pid, channel, payload):
        received.append(payload)
        event.set()

    listener_connection = await client.listen("hare_test_listen_channel_concurrent", callback)
    try:
        assert listener_connection is not client._pool

        for _ in range(5):
            rows_affected, rows = await client.execute("SELECT 1")
            assert rows_affected == 1
            assert rows[0][0] == 1

        event.clear()
        received.clear()
        await client.execute("NOTIFY hare_test_listen_channel_concurrent, 'still-alive'")
        await asyncio.wait_for(event.wait(), timeout=5)
        assert received == ["still-alive"]
    finally:
        await listener_connection.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_returns_a_dedicated_connection_caller_must_close(db_simple):
    """The returned Connection is genuinely usable directly (a real asyncpg connection object,
    not a pool proxy) and stays open until the caller explicitly closes it."""
    client = _asyncpg_client_or_skip()

    listener_connection = await client.listen("hare_test_listen_channel_dedicated", lambda *args: None)
    try:
        assert listener_connection.is_closed() is False
    finally:
        await listener_connection.close()
    assert listener_connection.is_closed() is True


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_ignores_pool_only_connection_parameters(db_simple):
    """Pool-only parameters (max_queries=, max_inactive_connection_lifetime=, min_size=, ...) must
    not reach the dedicated asyncpg.connect() connection, which rejects them with a TypeError."""
    _asyncpg_client_or_skip()
    shared = Connections.get("models")
    pooled_client = type(shared)(
        connection_name="pool_parameters_listen_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.host,
        port=shared.port,
        max_queries=100,
        max_inactive_connection_lifetime=10.0,
        min_size=1,
        max_size=3,
    )
    listener_connection = await pooled_client.listen("hare_test_listen_channel_pool_parameters", lambda *args: None)
    try:
        assert listener_connection.is_closed() is False
    finally:
        await listener_connection.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_with_unknown_connection_parameter_raises_configuration_error(db_simple):
    _asyncpg_client_or_skip()
    shared = Connections.get("models")
    # Rejected when the client is built - before listen() or any pool could hand it to asyncpg.
    with pytest.raises(ConfigurationError, match="definitely_not_a_connect_parameter"):
        type(shared)(
            connection_name="unknown_parameter_listen_test",
            user=shared.user,
            password=shared.password,
            database=shared.database,
            host=shared.host,
            port=shared.port,
            definitely_not_a_connect_parameter=1,
        )
