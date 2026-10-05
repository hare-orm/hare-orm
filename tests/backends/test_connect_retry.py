"""PostgresqlClient._create_pool_with_retry() - connect-time retry/backoff, shared by both
AsyncpgClient and RustPgClient (each declares its own RETRYABLE_CONNECT_EXCEPTIONS). Pure
unit tests against a real client instance with create_pool() monkeypatched - no real DB
connection needed."""

import pytest

from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient
from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient


def _make_asyncpg_client(**overrides):
    kwargs = {
        "connection_alias": "default",
        "user": "postgres",
        "password": "postgres",
        "database": "test",
        "host": "127.0.0.1",
        "port": 5432,
    }
    kwargs.update(overrides)
    return AsyncpgClient(**kwargs)


def test_connect_max_retries_defaults_to_zero():
    """Backward compatibility: a caller that never set connect_max_retries gets today's
    behavior - the very first connection failure propagates immediately."""
    client = _make_asyncpg_client()
    assert client.connect_max_retries == 0
    assert client.connect_retry_backoff_base_seconds == 0.1


def test_connect_max_retries_and_backoff_from_credentials():
    client = _make_asyncpg_client(connect_max_retries=3, connect_retry_backoff_base_seconds=0.05)
    assert client.connect_max_retries == 3
    assert client.connect_retry_backoff_base_seconds == 0.05


def test_pool_acquire_timeout_defaults_to_none():
    """Backward compatibility: a caller that never set pool_acquire_timeout gets today's
    behavior - acquire() waits indefinitely for a pool slot."""
    client = _make_asyncpg_client()
    assert client.pool_acquire_timeout is None


def test_pool_acquire_timeout_from_credentials_is_cast_to_float():
    client = _make_asyncpg_client(pool_acquire_timeout="2.5")
    assert client.pool_acquire_timeout == 2.5
    assert isinstance(client.pool_acquire_timeout, float)


@pytest.mark.asyncio
async def test_create_pool_with_retry_succeeds_after_transient_failures():
    client = _make_asyncpg_client(connect_max_retries=3, connect_retry_backoff_base_seconds=0.001)
    attempts = []

    async def flaky_create_pool(**kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionRefusedError("simulated: nobody listening")
        return "FAKE_POOL"

    client.create_pool = flaky_create_pool

    result = await client._create_pool_with_retry()

    assert result == "FAKE_POOL"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_create_pool_with_retry_retries_on_postgres_starting_up():
    """asyncpg.exceptions.CannotConnectNowError (SQLSTATE 57P03, "the database system is
    starting up") used to propagate immediately, unlike rust_pg's own retry logic
    (rust/pg/src/error.rs), which already treats the identical SQLSTATE as transient and
    retries - the exact same server condition retried with backoff on one driver but not the
    other, with identical connect_max_retries config."""
    import asyncpg

    client = _make_asyncpg_client(connect_max_retries=3, connect_retry_backoff_base_seconds=0.001)
    attempts = []

    async def starting_up_then_ready(**kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise asyncpg.exceptions.CannotConnectNowError("the database system is starting up")
        return "FAKE_POOL"

    client.create_pool = starting_up_then_ready

    result = await client._create_pool_with_retry()

    assert result == "FAKE_POOL"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_create_pool_with_retry_reraises_after_exhausting_retries():
    client = _make_asyncpg_client(connect_max_retries=2, connect_retry_backoff_base_seconds=0.001)
    attempts = []

    async def always_fails(**kwargs):
        attempts.append(1)
        raise ConnectionRefusedError("simulated: still down")

    client.create_pool = always_fails

    with pytest.raises(ConnectionRefusedError):
        await client._create_pool_with_retry()

    # The initial attempt plus connect_max_retries retries.
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_create_pool_with_retry_does_not_retry_non_retryable_exception():
    client = _make_asyncpg_client(connect_max_retries=5, connect_retry_backoff_base_seconds=0.001)
    attempts = []

    async def raises_value_error(**kwargs):
        attempts.append(1)
        raise ValueError("not a connection error")

    client.create_pool = raises_value_error

    with pytest.raises(ValueError):
        await client._create_pool_with_retry()

    assert len(attempts) == 1


def test_asyncpg_and_rust_pg_declare_distinct_retryable_exceptions():
    """Each driver raises its own exception type for a failed connect - retry must be
    keyed off the driver actually in use, not a single shared type."""
    import asyncpg

    from rust.native import pg

    assert OSError in AsyncpgClient.RETRYABLE_CONNECT_EXCEPTIONS
    assert asyncpg.exceptions.PostgresConnectionError in AsyncpgClient.RETRYABLE_CONNECT_EXCEPTIONS
    # SQLSTATE 57P01-57P04 ("admin shutdown"/"crash recovery"/"starting up"/"database dropped") -
    # rust_pg's own pg.ConnectionError already covers this identical range (rust/pg/src/error.rs),
    # so asyncpg must too, for the two drivers to actually agree on which failures are transient.
    assert asyncpg.exceptions.AdminShutdownError in AsyncpgClient.RETRYABLE_CONNECT_EXCEPTIONS
    assert asyncpg.exceptions.CrashShutdownError in AsyncpgClient.RETRYABLE_CONNECT_EXCEPTIONS
    assert asyncpg.exceptions.CannotConnectNowError in AsyncpgClient.RETRYABLE_CONNECT_EXCEPTIONS
    assert asyncpg.exceptions.DatabaseDroppedError in AsyncpgClient.RETRYABLE_CONNECT_EXCEPTIONS
    assert RustPgClient.RETRYABLE_CONNECT_EXCEPTIONS == (pg.ConnectionError,)
