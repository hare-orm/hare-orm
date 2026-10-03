"""PostgresqlClient._execute_read_query_with_retry() - mid-query connection-loss retry/backoff
for a statically read-only query (AwaitableQuery.is_read_only), shared by both AsyncpgClient
and RustPgClient. Pure unit tests against a real client instance with _translate_exceptions
monkeypatched - no real DB connection needed, mirroring tests/backends/test_connect_retry.py's
own pattern for PostgresqlClient._create_pool_with_retry(). See tests/backends/
test_postgres_common_client.py for translate_exceptions' own contextvar-gating (only a
read-only, query-executing, non-transactional call is ever retried) and tests/backends/
test_postgres.py for the live pg_terminate_backend proof against a real Postgres."""

import pytest

from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient
from hare.exceptions import DBConnectionError, OperationalError
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted


def _make_client(**overrides):
    kwargs = {
        "connection_name": "default",
        "user": "postgres",
        "password": "postgres",
        "database": "test",
        "host": "127.0.0.1",
        "port": 5432,
    }
    kwargs.update(overrides)
    return AsyncpgClient(**kwargs)


def test_read_retry_max_retries_defaults_to_zero():
    """Backward compatibility: a caller that never set read_retry_max_retries gets today's
    behavior - a connection lost mid-query raises DBConnectionError immediately."""
    client = _make_client()
    assert client.read_retry_max_retries == 0
    assert client.read_retry_backoff_base_seconds == 0.1


def test_read_retry_max_retries_and_backoff_from_credentials():
    client = _make_client(read_retry_max_retries=3, read_retry_backoff_base_seconds=0.05)
    assert client.read_retry_max_retries == 3
    assert client.read_retry_backoff_base_seconds == 0.05


@pytest.mark.asyncio
async def test_execute_read_query_with_retry_succeeds_after_transient_connection_loss():
    client = _make_client(read_retry_max_retries=3, read_retry_backoff_base_seconds=0.001)
    attempts = []

    async def fake_translate_exceptions(func, *args, **kwargs):
        return await func(client, *args, **kwargs)

    client._translate_exceptions = fake_translate_exceptions

    async def flaky(_self):
        attempts.append(1)
        if len(attempts) < 3:
            raise DBConnectionError("simulated: connection lost mid-query")
        return "FAKE_ROWS"

    result = await client._execute_read_query_with_retry(flaky)

    assert result == "FAKE_ROWS"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_execute_read_query_with_retry_reraises_after_exhausting_retries():
    client = _make_client(read_retry_max_retries=2, read_retry_backoff_base_seconds=0.001)
    attempts = []

    async def fake_translate_exceptions(func, *args, **kwargs):
        return await func(client, *args, **kwargs)

    client._translate_exceptions = fake_translate_exceptions

    async def always_fails(_self):
        attempts.append(1)
        raise DBConnectionError("simulated: still down")

    with pytest.raises(DBConnectionError):
        await client._execute_read_query_with_retry(always_fails)

    # The initial attempt plus read_retry_max_retries retries.
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_execute_read_query_with_retry_does_not_retry_operational_error():
    """A cancelled-but-still-usable connection (statement_timeout/pg_cancel_backend) translates
    to OperationalError, not DBConnectionError - retrying it would be pointless (the same
    connection already works fine for the next statement) and could mask an intentional
    cancellation, so only DBConnectionError triggers a retry here."""
    client = _make_client(read_retry_max_retries=5, read_retry_backoff_base_seconds=0.001)
    attempts = []

    async def fake_translate_exceptions(func, *args, **kwargs):
        try:
            return await func(client, *args, **kwargs)
        except ValueError as exc:
            raise OperationalError(exc)

    client._translate_exceptions = fake_translate_exceptions

    async def query_canceled(_self):
        attempts.append(1)
        raise ValueError("simulated: query_canceled")

    with pytest.raises(OperationalError):
        await client._execute_read_query_with_retry(query_canceled)

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_execute_read_query_with_retry_records_each_attempt_to_instrumentation():
    """Each attempt - failed or successful - must reach QueryInstrumentation separately, with its
    own honest duration. Before this fix, translate_exceptions() recorded once for the WHOLE
    retried call (its outer start/finally), so two failed attempts before a successful third were
    completely invisible to instrumentation - no failure record, and the one "success" record's
    duration silently absorbed both backoff sleeps. See translate_exceptions()'s `record_here`
    flag (hare.dialects.postgresql.client) - it skips its own top-level record whenever it
    dispatches into this method, making this the only place a retried read gets recorded."""
    client = _make_client(read_retry_max_retries=3, read_retry_backoff_base_seconds=0.05)
    attempts = []

    async def fake_translate_exceptions(func, *args, **kwargs):
        return await func(client, *args, **kwargs)

    client._translate_exceptions = fake_translate_exceptions

    async def flaky(_self):
        attempts.append(1)
        if len(attempts) < 3:
            raise DBConnectionError("simulated: connection lost mid-query")
        return "FAKE_ROWS"

    calls = []

    def hook(event):
        sql, params, duration_ms, exception = event.sql, event.params, event.duration_ms, event.error
        calls.append((sql, params, duration_ms, exception))

    Observers.observe(QueryExecuted, hook)
    try:
        result = await client._execute_read_query_with_retry(flaky, sql="SELECT 1", params=[42])
        # Hooks now dispatch in the background (never awaited by the query call itself) - drain
        # every in-flight one before asserting on their effect, or this is flaky.
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, hook)

    assert result == "FAKE_ROWS"
    # 2 failed attempts + 1 successful attempt - not the single record the pre-fix code produced.
    assert len(calls) == 3

    for sql, params, duration_ms, exception in calls[:2]:
        assert (sql, params) == ("SELECT 1", [42])
        assert isinstance(exception, DBConnectionError)
        # A failed attempt does no real work here, so its own honest duration must stay far
        # below the 50ms backoff delay that follows it - the pre-fix bug would have folded that
        # sleep (and the other attempt's) into a single reported duration well past this bound.
        assert duration_ms < 40

    sql, params, duration_ms, exception = calls[2]
    assert (sql, params) == ("SELECT 1", [42])
    assert exception is None
