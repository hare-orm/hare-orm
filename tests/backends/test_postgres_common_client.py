"""Unit tests for hare.dialects.postgresql.client's shared translate_exceptions decorator
(used by both AsyncpgClient and RustPgClient via their own get_driver_error override) -
pure decorator-level tests against a fake client, no real DB connection needed. See
tests/backends/test_sqlite_client.py for the equivalent sqlite-side tests, and
tests/backends/test_read_query_retry.py for the shared per-statement retry loop."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from hare.dialects.base.client.enums import StatementForm
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.client import PostgresqlClient
from hare.exceptions import DBConnectionError, OperationalError
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.sql.terms.parameters.query_parameters import QueryParameters


class _FakeClient:
    command_timeout = None
    connection_alias = "default"
    is_transaction_client = False
    password_provider = None
    read_retry_max_retries = 0
    transaction_pooling = False

    def get_driver_error(self, error, call_arguments):
        return OperationalError(error) if isinstance(error, ValueError) else None


@pytest.mark.asyncio
async def test_translate_exceptions_attaches_sql_and_params_on_failure():
    @PostgresqlClient.translate_exceptions(StatementForm.SQL)
    async def execute(self, query, values=None):
        raise ValueError("boom")

    with pytest.raises(OperationalError) as exc_info:
        await execute(_FakeClient(), "SELECT * FROM t WHERE id=$1", values=[42])

    assert exc_info.value.sql == "SELECT * FROM t WHERE id=$1"
    assert exc_info.value.parameters == [42]


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_translate_exceptions_does_not_attach_sql_for_non_query_methods():
    """create_connection (etc) aren't query-executing - their positional args aren't a
    (sql, params) pair, so nothing should be attached even though they can still raise a
    translated exception."""

    @PostgresqlClient.translate_exceptions(StatementForm.CONTROL)
    async def create_connection(self, with_db):
        raise ValueError("boom")

    with pytest.raises(OperationalError) as exc_info:
        await create_connection(_FakeClient(), True)

    assert exc_info.value.sql is None
    assert exc_info.value.parameters is None


@pytest.mark.asyncio
async def test_translate_exceptions_fires_query_hook_on_success_and_failure():
    calls = []

    def hook(event):
        sql, params, duration_ms, exception = event.sql, event.parameters, event.duration_ms, event.error
        calls.append((sql, params, duration_ms, exception))

    Observers.observe(QueryExecuted, hook)
    try:

        @PostgresqlClient.translate_exceptions(StatementForm.SQL)
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

    @PostgresqlClient.translate_exceptions(StatementForm.SQL)
    async def execute(self, query, values=None):
        return "ok"

    with caplog.at_level("DEBUG", logger="hare.db_client"):
        await execute(_FakeClient(), "SELECT 1")

    assert any("Slow query" in message for message in caplog.messages)


class _RetryFakeClient(_FakeClient):
    """Driver error translation and retry settings without a server."""

    read_retry_max_retries = 3
    read_retry_backoff_base_seconds = 0.001
    log = logging.getLogger("test_translate_exceptions_read_retry")

    def get_driver_error(self, error, call_arguments):
        if isinstance(error, ConnectionAbortedError):
            return DBConnectionError(error)
        return OperationalError(error) if isinstance(error, ValueError) else None


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_explicit_read_retry_does_not_spread_to_nested_wrapper_writes():
    client = _RetryFakeClient()
    client.dialect = DialectRegistry.get_dialect("postgresql")
    client.read_retry_backoff_base_seconds = 0
    reads, writes = [], []

    @PostgresqlClient.translate_exceptions(StatementForm.SQL)
    async def read_rows(self, query, values=None):
        reads.append(query)
        if len(reads) == 1:
            raise ConnectionAbortedError("lost read")
        return "rows"

    @PostgresqlClient.translate_exceptions(StatementForm.SQL)
    async def write_rows(self, query, values=None):
        writes.append(query)
        raise ConnectionAbortedError("unknown write outcome")

    async def around(call, proceed):
        if call.method_name == "read_rows":
            with pytest.raises(DBConnectionError):
                await write_rows(client, "UPDATE book SET title = 'new'")
        return await proceed()

    wrapper = SimpleNamespace(around=around)
    Observers.wrap_queries(wrapper)
    try:
        assert await read_rows(client, "SELECT 1", retry_read=True) == "rows"
    finally:
        Observers.unwrap_queries(wrapper)
    assert len(reads) == 2
    assert len(writes) == 1


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_cancelled_statement_reports_cancellation_once():
    @PostgresqlClient.translate_exceptions(StatementForm.SQL)
    async def cancelled(self, query, values=None):
        raise asyncio.CancelledError()

    events = []
    Observers.observe(QueryExecuted, events.append)
    try:
        with pytest.raises(asyncio.CancelledError):
            await cancelled(_FakeClient(), "SELECT 1")
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, events.append)
    assert len(events) == 1
    assert isinstance(events[0].error, asyncio.CancelledError)


@pytest.mark.database_independent
@pytest.mark.parametrize("form", list(StatementForm))
@pytest.mark.asyncio
async def test_only_single_statement_form_can_accept_read_retry(form):
    client = _RetryFakeClient()
    client.dialect = DialectRegistry.get_dialect("postgresql")
    client.read_retry_max_retries = 1
    client.read_retry_backoff_base_seconds = 0
    attempts = []

    @PostgresqlClient.translate_exceptions(form)
    async def execute(self, query, values=None):
        attempts.append(1)
        raise ConnectionAbortedError("connection lost")

    with pytest.raises(DBConnectionError):
        await execute(client, "book", ["id"], retry_read=True)
    assert len(attempts) == (2 if form is StatementForm.SQL else 1)


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_each_retry_event_hides_sensitive_driver_error_and_parameters():
    client = _RetryFakeClient()
    client.read_retry_max_retries = 1
    client.read_retry_backoff_base_seconds = 0
    secret = "sensitive-input"
    events = []

    @PostgresqlClient.translate_exceptions(StatementForm.SQL)
    async def execute(self, query, values=None):
        raise ConnectionAbortedError(secret)

    Observers.observe(QueryExecuted, events.append)
    try:
        with pytest.raises(DBConnectionError):
            await execute(client, "SELECT $1", QueryParameters([secret], [0]), retry_read=True)
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, events.append)
    assert len(events) == 2
    for event in events:
        assert event.parameters == ["<hidden>"]
        assert secret not in str(event.error)
        assert event.error.__context__ is None


@pytest.mark.asyncio
async def test_translate_exceptions_retries_read_only_query_on_connection_loss():
    """A declared SQL invocation retries only with its explicit read permission."""
    client = _RetryFakeClient()
    attempts = []

    @PostgresqlClient.translate_exceptions(StatementForm.SQL)
    async def execute(self, query, values=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionAbortedError("simulated: connection lost mid-query")
        return "ok"

    result = await execute(client, "SELECT 1", retry_read=True)

    assert result == "ok"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_translate_exceptions_does_not_retry_without_explicit_permission():
    """SQL returning rows does not by itself authorize retrying after connection loss."""
    client = _RetryFakeClient()
    attempts = []

    @PostgresqlClient.translate_exceptions(StatementForm.SQL)
    async def execute(self, query, values=None):
        attempts.append(1)
        raise ConnectionAbortedError("simulated: connection lost mid-query")

    with pytest.raises(DBConnectionError):
        await execute(client, "SELECT 1")

    assert len(attempts) == 1


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_translate_exceptions_does_not_retry_non_query_executing_methods():
    """Transaction control cannot opt into read retry."""
    client = _RetryFakeClient()
    attempts = []

    @PostgresqlClient.translate_exceptions(StatementForm.CONTROL)
    async def commit(self):
        attempts.append(1)
        raise ConnectionAbortedError("simulated: connection lost mid-commit")

    with pytest.raises(DBConnectionError):
        await commit(client, retry_read=True)

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_translate_exceptions_does_not_retry_inside_a_transaction():
    """Retrying against a connection pinned to an open transaction would either re-run the read
    outside the transaction's isolation/locks entirely, or just fail again on the same dead
    connection - neither is useful, so a TransactionClient is never retried even when
    retry_read=True is passed."""

    class _RetryFakeTransactionalClient(_RetryFakeClient):
        is_transaction_client = True

        def _mark_statement_failed(self):
            pass

        def _check_statement_allowed(self):
            pass

    client = _RetryFakeTransactionalClient()
    attempts = []

    @PostgresqlClient.translate_exceptions(StatementForm.SQL)
    async def execute(self, query, values=None):
        attempts.append(1)
        raise ConnectionAbortedError("simulated: connection lost mid-query")

    with pytest.raises(DBConnectionError):
        await execute(client, "SELECT 1", retry_read=True)

    assert len(attempts) == 1
