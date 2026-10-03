"""Unit tests for hare.dialects.postgresql.client's shared translate_exceptions decorator
(used by both AsyncpgClient and RustPgClient via their own get_driver_error override) -
pure decorator-level tests against a fake client, no real DB connection needed. See
tests/backends/test_sqlite_client.py for the equivalent sqlite-side tests, and
tests/backends/test_read_query_retry.py for PostgresqlClient._execute_read_query_with_retry()
itself (the retry loop translate_exceptions dispatches into below)."""

import logging

import pytest

from hare.dialects.base.client import retryable_read_query_active
from hare.dialects.postgresql.client import PostgresqlClient
from hare.exceptions import DBConnectionError, OperationalError
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted


class _FakeClient:
    command_timeout = None
    connection_name = "default"
    is_transaction_client = False
    read_retry_max_retries = 0

    def get_driver_error(self, error, call_arguments):
        return OperationalError(error) if isinstance(error, ValueError) else None


@pytest.mark.asyncio
async def test_translate_exceptions_attaches_sql_and_params_on_failure():
    @PostgresqlClient.translate_exceptions
    async def execute(self, query, values=None):
        raise ValueError("boom")

    with pytest.raises(OperationalError) as exc_info:
        await execute(_FakeClient(), "SELECT * FROM t WHERE id=$1", values=[42])

    assert exc_info.value.sql == "SELECT * FROM t WHERE id=$1"
    assert exc_info.value.params == [42]


@pytest.mark.asyncio
async def test_translate_exceptions_does_not_attach_sql_for_non_query_methods():
    """create_connection (etc) aren't query-executing - their positional args aren't a
    (sql, params) pair, so nothing should be attached even though they can still raise a
    translated exception."""

    @PostgresqlClient.translate_exceptions
    async def create_connection(self, with_db):
        raise ValueError("boom")

    with pytest.raises(OperationalError) as exc_info:
        await create_connection(_FakeClient(), True)

    assert exc_info.value.sql is None
    assert exc_info.value.params is None


@pytest.mark.asyncio
async def test_translate_exceptions_fires_query_hook_on_success_and_failure():
    calls = []

    def hook(event):
        sql, params, duration_ms, exception = event.sql, event.params, event.duration_ms, event.error
        calls.append((sql, params, duration_ms, exception))

    Observers.observe(QueryExecuted, hook)
    try:

        @PostgresqlClient.translate_exceptions
        async def execute(self, query, values=None):
            if query.startswith("FAIL"):
                raise ValueError("boom")
            return "ok"

        assert await execute(_FakeClient(), "SELECT 1") == "ok"
        # Hooks now dispatch in the background (never awaited by the query call itself) - drain
        # after EACH call, not just at the end, or the two calls' background tasks can finish in
        # either order and calls[0]/calls[1] below stop corresponding to call order.
        await Observers.wait_for_pending()

        with pytest.raises(OperationalError):
            await execute(_FakeClient(), "FAIL SELECT", values=[1])
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, hook)

    assert len(calls) == 2

    sql, params, duration_ms, exception = calls[0]
    assert (sql, params, exception) == ("SELECT 1", None, None)
    assert duration_ms >= 0

    sql, params, duration_ms, exception = calls[1]
    assert (sql, params) == ("FAIL SELECT", [1])
    assert isinstance(exception, OperationalError)
    assert duration_ms >= 0


@pytest.mark.asyncio
async def test_translate_exceptions_logs_slow_query(caplog, monkeypatch):
    monkeypatch.setattr(Observers, "slow_query_threshold_ms", -1.0)

    @PostgresqlClient.translate_exceptions
    async def execute(self, query, values=None):
        return "ok"

    with caplog.at_level("DEBUG", logger="hare.db_client"):
        await execute(_FakeClient(), "SELECT 1")

    assert any("Slow query" in message for message in caplog.messages)


class _RetryFakeClient(_FakeClient):
    """Adds the retry-specific attributes/method translate_exceptions' new retry branch needs
    (PostgresqlClient._execute_read_query_with_retry itself is exercised directly against a
    real client in tests/backends/test_read_query_retry.py - this only needs to prove
    translate_exceptions DISPATCHES into it under the right conditions)."""

    read_retry_max_retries = 3
    read_retry_backoff_base_seconds = 0.001
    log = logging.getLogger("test_translate_exceptions_read_retry")
    _execute_read_query_with_retry = PostgresqlClient._execute_read_query_with_retry
    _translate_exceptions = PostgresqlClient._translate_exceptions

    def get_driver_error(self, error, call_arguments):
        if isinstance(error, ConnectionAbortedError):
            return DBConnectionError(error)
        return OperationalError(error) if isinstance(error, ValueError) else None


@pytest.mark.asyncio
async def test_translate_exceptions_retries_read_only_query_on_connection_loss():
    """The new parallel retry mechanism (translate_exceptions -> _execute_read_query_with_retry)
    only engages when retryable_read_query_active is set - i.e. the currently-executing query's
    SQL-builder class is statically read-only (AwaitableQuery.is_read_only)."""
    client = _RetryFakeClient()
    attempts = []

    @PostgresqlClient.translate_exceptions
    async def execute(self, query, values=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionAbortedError("simulated: connection lost mid-query")
        return "ok"

    token = retryable_read_query_active.set(True)
    try:
        result = await execute(client, "SELECT 1")
    finally:
        retryable_read_query_active.reset(token)

    assert result == "ok"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_translate_exceptions_does_not_retry_when_context_inactive():
    """Without retryable_read_query_active set (a write query, or code outside any AwaitableQuery
    at all), a connection lost mid-query still raises DBConnectionError immediately - exactly
    today's behavior, no retry."""
    client = _RetryFakeClient()
    attempts = []

    @PostgresqlClient.translate_exceptions
    async def execute(self, query, values=None):
        attempts.append(1)
        raise ConnectionAbortedError("simulated: connection lost mid-query")

    with pytest.raises(DBConnectionError):
        await execute(client, "SELECT 1")

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_translate_exceptions_does_not_retry_non_query_executing_methods():
    """commit()/rollback() (and any other non-query-executing method) must never be retried by
    this mechanism even if retryable_read_query_active is somehow set - only a real
    QUERY_EXECUTING_METHOD_NAMES method is eligible."""
    client = _RetryFakeClient()
    attempts = []

    @PostgresqlClient.translate_exceptions
    async def commit(self):
        attempts.append(1)
        raise ConnectionAbortedError("simulated: connection lost mid-commit")

    token = retryable_read_query_active.set(True)
    try:
        with pytest.raises(DBConnectionError):
            await commit(client)
    finally:
        retryable_read_query_active.reset(token)

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_translate_exceptions_does_not_retry_inside_a_transaction():
    """Retrying against a connection pinned to an open transaction would either re-run the read
    outside the transaction's isolation/locks entirely, or just fail again on the same dead
    connection - neither is useful, so a TransactionClient is never retried even when
    retryable_read_query_active is set."""

    class _RetryFakeTransactionalClient(_RetryFakeClient):
        is_transaction_client = True

        def _mark_statement_failed(self):
            pass

        def _check_statement_allowed(self):
            pass

    client = _RetryFakeTransactionalClient()
    attempts = []

    @PostgresqlClient.translate_exceptions
    async def execute(self, query, values=None):
        attempts.append(1)
        raise ConnectionAbortedError("simulated: connection lost mid-query")

    token = retryable_read_query_active.set(True)
    try:
        with pytest.raises(DBConnectionError):
            await execute(client, "SELECT 1")
    finally:
        retryable_read_query_active.reset(token)

    assert len(attempts) == 1


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("UPDATE t SET a = 1", True),
        ("/* audit */ UPDATE t SET a = 1", True),
        ("/* outer /* nested */ still comment */ DELETE FROM t", True),
        ("-- audit\nUPDATE t SET a = 1", True),
        ("WITH x AS MATERIALIZED (SELECT id FROM t) UPDATE t SET a = 1 WHERE id IN (SELECT id FROM x)", True),
        ('WITH x AS NOT MATERIALIZED (SELECT 1), "y"(a) AS (SELECT 2) DELETE FROM t', True),
        ("UPDATE t SET name = 'returning'", True),
        ('UPDATE t SET "returning" = 1', True),
        ("UPDATE t SET a = $$ returning $$", True),
        ("UPDATE t SET a = $tag$ it's returning $tag$", True),
        (r"UPDATE t SET a = E'x\' RETURNING'", True),
        ("WITH d AS (DELETE FROM t RETURNING id) UPDATE u SET a = 1 WHERE id IN (SELECT id FROM d)", True),
        ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", True),
        ("UPDATE t SET a = 1 RETURNING id", False),
        ("/* audit */ UPDATE t SET a = 1 RETURNING id", False),
        ("WITH d AS (DELETE FROM t RETURNING id) SELECT * FROM d", False),
        ("SELECT * FROM t WHERE a = 'UPDATE'", False),
        ("(SELECT 1)", False),
        ("", False),
    ],
)
def test_is_write_without_returning_ignores_comments_ctes_and_quoted_text(query, expected):
    assert PostgresqlClient._is_write_without_returning(query) is expected
