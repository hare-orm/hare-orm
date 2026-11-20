"""Explicit statement retry and per-attempt events, without a database server."""

from types import SimpleNamespace

import pytest

from hare.dialects.base.client import statement_execution
from hare.dialects.base.client.enums import StatementForm
from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient
from hare.exceptions import DBConnectionError, OperationalError
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers import observers
from hare.instrumentation.observers.observers import Observers
from tests.utils.manual_clock import ManualClock

pytestmark = pytest.mark.database_independent


@pytest.fixture(autouse=True)
def retry_clock(monkeypatch):
    clock = ManualClock()
    monkeypatch.setattr(statement_execution, "time", clock)
    monkeypatch.setattr(
        statement_execution, "asyncio", SimpleNamespace(**{**vars(statement_execution.asyncio), "sleep": clock.sleep})
    )
    monkeypatch.setattr(observers, "time", clock)
    return clock


def _make_client(**overrides):
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


@pytest.mark.database_independent
def test_read_retry_max_retries_defaults_to_zero():
    """Backward compatibility: a caller that never set read_retry_max_retries gets today's
    behavior - a connection lost mid-query raises DBConnectionError immediately."""
    client = _make_client()
    assert client.read_retry_max_retries == 0
    assert client.read_retry_backoff_base_seconds == 0.1


@pytest.mark.database_independent
def test_read_retry_max_retries_and_backoff_from_credentials():
    client = _make_client(read_retry_max_retries=3, read_retry_backoff_base_seconds=0.05)
    assert client.read_retry_max_retries == 3
    assert client.read_retry_backoff_base_seconds == 0.05


@pytest.mark.asyncio
async def test_execute_read_query_with_retry_succeeds_after_transient_connection_loss():
    client = _make_client(read_retry_max_retries=3, read_retry_backoff_base_seconds=0.001)
    attempts = []

    @AsyncpgClient.translate_exceptions(StatementForm.SQL)
    async def flaky(_self, query, values=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise DBConnectionError("simulated: connection lost mid-query")
        return "FAKE_ROWS"

    result = await flaky(client, "SELECT 1", retry_read=True)

    assert result == "FAKE_ROWS"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_execute_read_query_with_retry_reraises_after_exhausting_retries():
    client = _make_client(read_retry_max_retries=2, read_retry_backoff_base_seconds=0.001)
    attempts = []

    @AsyncpgClient.translate_exceptions(StatementForm.SQL)
    async def always_fails(_self, query, values=None):
        attempts.append(1)
        raise DBConnectionError("simulated: still down")

    with pytest.raises(DBConnectionError):
        await always_fails(client, "SELECT 1", retry_read=True)

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

    @AsyncpgClient.translate_exceptions(StatementForm.SQL)
    async def query_canceled(_self, query, values=None):
        attempts.append(1)
        raise OperationalError("simulated: query_canceled")

    with pytest.raises(OperationalError):
        await query_canceled(client, "SELECT 1", retry_read=True)

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_execute_read_query_with_retry_records_each_attempt_to_instrumentation(retry_clock):
    """Each attempt reports its duration and translated error, excluding backoff sleeps."""
    client = _make_client(read_retry_max_retries=3, read_retry_backoff_base_seconds=0.05)
    attempts = []

    @AsyncpgClient.translate_exceptions(StatementForm.SQL)
    async def flaky(_self, query, values=None):
        attempts.append(1)
        retry_clock.advance(len(attempts) / 8)
        if len(attempts) < 3:
            raise DBConnectionError("simulated: connection lost mid-query")
        return "FAKE_ROWS"

    calls = []

    def hook(event):
        sql, params, duration_ms, exception = event.sql, event.parameters, event.duration_ms, event.error
        calls.append((sql, params, duration_ms, exception))

    Observers.observe(QueryExecuted, hook)
    try:
        result = await flaky(client, "SELECT 1", [42], retry_read=True)
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

    assert [call[2] for call in calls] == pytest.approx([125, 250, 375])
    assert retry_clock.delays == [0.05, 0.1]

    sql, params, duration_ms, exception = calls[2]
    assert (sql, params) == ("SELECT 1", [42])
    assert exception is None
