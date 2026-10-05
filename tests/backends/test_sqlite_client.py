"""SqliteClient must split constructor kwargs into "consumed by a base
class/get_client_class()" vs "sqlite PRAGMA" via the explicit
_NON_PRAGMA_KWARGS allowlist, not by having to
remember every non-pragma kwarg at the .pop() call site."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from hare.contrib.test import requires_features
from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.dialects.sqlite.client import sqlite_client as sqlite_client_module
from hare.dialects.sqlite.drivers.aiosqlite.client import (
    AiosqliteClient,
    AiosqliteClientWithRegexpSupport,
)
from hare.exceptions import DBConnectionError, IntegrityError, OperationalError, TransactionManagementError
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament


def test_pragmas_exclude_base_client_kwargs():
    client = AiosqliteClient(file_path=":memory:", connection_alias="default", fetch_inserted=False)
    assert "connection_alias" not in client.pragmas
    assert "fetch_inserted" not in client.pragmas


def test_pragmas_exclude_install_regexp_functions():
    """Regression: install_regexp_functions previously leaked through as a
    bogus "PRAGMA install_regexp_functions=True" (harmless only because
    sqlite silently ignores unknown pragma names)."""
    client = AiosqliteClientWithRegexpSupport(
        file_path=":memory:", connection_alias="default", install_regexp_functions=True
    )
    assert "install_regexp_functions" not in client.pragmas


def test_pragmas_include_actual_pragma_kwargs():
    client = AiosqliteClient(file_path=":memory:", connection_alias="default", cache_size=-2000)
    assert client.pragmas["cache_size"] == -2000
    assert client.pragmas["journal_mode"] == "WAL"


@pytest.mark.asyncio
async def test_a_double_quoted_name_of_no_column_is_refused_not_read_as_a_string():
    """SQLite reads a double-quoted name that is no column as a string literal unless its DQS
    options are off - an index or a condition on a misspelled or dropped column then silently
    worked on a constant."""
    client = AiosqliteClient(file_path=":memory:", connection_alias="default")
    await client.create_connection(with_db=True)
    try:
        await client.execute_script('CREATE TABLE "quoted" ("id" INTEGER PRIMARY KEY)')
        with pytest.raises(OperationalError, match="no such column"):
            await client.execute('SELECT "no_such_column" FROM "quoted"')
        with pytest.raises(OperationalError, match="no_such_column"):
            await client.execute_script('CREATE INDEX "quoted_missing" ON "quoted" ("no_such_column")')
    finally:
        await client.close()


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_begin_is_shielded_from_cancellation(db):
    """SqliteTransactionClient.begin()/savepoint() used to send the real BEGIN/SAVEPOINT
    unshielded, unlike commit()/rollback()/release_savepoint()/savepoint_rollback() in the same
    file - aiosqlite queues a call onto a background thread that runs it to completion regardless
    of whether the asyncio future awaiting it was cancelled, so a cancellation landing mid-await
    could leave a real, untracked BEGIN/SAVEPOINT on the shared connection while this wrapper's
    own bookkeeping never learned it happened - the same class of bug already fixed for asyncpg/
    rust_pg's own begin()/savepoint(). Confirms the wiring: begin() now genuinely routes through
    _run_shielded_from_cancellation, not just that the shared shielding mechanism itself works
    (already covered by tests/backends/test_transactional_client_shielding.py)."""
    from unittest import mock

    shielded_calls: list[object] = []
    real_shield = TransactionEnding.run_shielded_from_cancellation

    async def spying_shield(coroutine, **kwargs):
        shielded_calls.append(coroutine)
        return await real_shield(coroutine, **kwargs)

    with mock.patch.object(TransactionEnding, "run_shielded_from_cancellation", staticmethod(spying_shield)):
        async with Transactions.atomic():
            # The BEGIN goes out ahead of the transaction's first statement.
            await Tournament.objects.count()

    # commit() alone already accounts for 1 shielded call even before this fix - begin() must
    # add a SECOND one of its own (the real BEGIN), not just ride along on commit()'s
    # pre-existing shielding.
    assert len(shielded_calls) >= 2, "begin() must route its real BEGIN through _run_shielded_from_cancellation too"


@pytest.mark.asyncio
async def test_translate_exceptions_forwards_keyword_arguments():
    """translate_exceptions()'s wrapper only accepted (self, query, *args) - unlike
    postgres_common's equivalent, which forwards **kwargs too - so a decorated call using a
    keyword argument (matching every wrapped method's own `values=`-style signature) crashed with
    a TypeError at the wrapper, not just silently dropped the kwarg."""
    calls = []

    @AiosqliteClient.translate_exceptions
    async def fake_query(self, query, values=None):
        calls.append((query, values))
        return "ok"

    result = await fake_query(
        SimpleNamespace(
            connection_alias="default",
            is_transaction_client=False,
            driver_errors=AiosqliteClient.driver_errors,
            has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
        ),
        "SELECT 1",
        values=[1, 2, 3],
    )
    assert result == "ok"
    assert calls == [("SELECT 1", [1, 2, 3])]


@pytest.mark.asyncio
async def test_translate_exceptions_attaches_sql_and_params_on_failure():
    @AiosqliteClient.translate_exceptions
    async def execute(self, query, values=None):
        raise sqlite3.OperationalError("no such table: t")

    with pytest.raises(OperationalError) as exc_info:
        await execute(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            ),
            "SELECT * FROM t",
            values=[1],
        )

    assert exc_info.value.sql == "SELECT * FROM t"
    assert exc_info.value.parameters == [1]

    @AiosqliteClient.translate_exceptions
    async def execute_described(self, query, values=None):
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    with pytest.raises(IntegrityError) as integrity_exc_info:
        await execute_described(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            ),
            "INSERT INTO t (id) VALUES (?)",
            [1],
        )

    assert integrity_exc_info.value.sql == "INSERT INTO t (id) VALUES (?)"
    assert integrity_exc_info.value.parameters == [1]


@pytest.mark.asyncio
async def test_translate_exceptions_translates_the_rest_of_sqlite3_errors():
    """sqlite3.Error's other direct subclasses (ProgrammingError/DataError/InternalError/
    NotSupportedError/InterfaceError - everything besides OperationalError/IntegrityError,
    already covered above) used to propagate completely untranslated: confirmed live that a
    wrong bind-parameter count or an unsupported bind type raised a raw
    sqlite3.ProgrammingError, invisible to any caller catching hare.exceptions.*."""

    @AiosqliteClient.translate_exceptions
    async def execute(self, query, values=None):
        raise sqlite3.ProgrammingError("Incorrect number of bindings supplied")

    with pytest.raises(OperationalError) as programming_exc_info:
        await execute(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            ),
            "SELECT ?, ?",
            values=[1],
        )
    assert programming_exc_info.value.sql == "SELECT ?, ?"

    @AiosqliteClient.translate_exceptions
    async def execute_dicts(self, query, values=None):
        raise sqlite3.DataError("string or blob too big")

    with pytest.raises(OperationalError):
        await execute_dicts(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            ),
            "SELECT 1",
            values=None,
        )

    @AiosqliteClient.translate_exceptions
    async def execute_described(self, query, values=None):
        raise sqlite3.InternalError("cursor invalid")

    with pytest.raises(OperationalError):
        await execute_described(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            ),
            "SELECT 1",
            values=None,
        )

    @AiosqliteClient.translate_exceptions
    async def execute_many(self, query, values=None):
        raise sqlite3.NotSupportedError("not supported")

    with pytest.raises(OperationalError):
        await execute_many(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            ),
            "SELECT 1",
            values=None,
        )

    # InterfaceError -> DBConnectionError, not OperationalError - mirrors AsyncpgClient's own
    # identical mapping for asyncpg.InterfaceError (both mean "this connection/cursor object
    # itself is no longer usable", not a problem with the query text).
    @AiosqliteClient.translate_exceptions
    async def execute_script(self, query, values=None):
        raise sqlite3.InterfaceError("Cannot operate on a closed database")

    with pytest.raises(DBConnectionError) as interface_exc_info:
        await execute_script(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            ),
            "SELECT 1",
            values=None,
        )
    assert interface_exc_info.value.sql == "SELECT 1"

    # sqlite3.DatabaseError itself (not one of the 6 named leaf subclasses above) - confirmed
    # live via a genuinely corrupted database file ("database disk image is malformed"), which
    # previously propagated completely untranslated despite meaning exactly the type of failure
    # application code most wants a catchable hare.exceptions.OperationalError for.
    @AiosqliteClient.translate_exceptions
    async def execute(self, query, values=None):
        raise sqlite3.DatabaseError("database disk image is malformed")

    with pytest.raises(OperationalError) as database_exc_info:
        await execute(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            ),
            "SELECT 1",
            values=None,
        )
    assert database_exc_info.value.sql == "SELECT 1"


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_non_sqlite3_binding_errors_are_translated_and_reported_to_hooks(db):
    """sqlite3 raises OverflowError (an int beyond int64) outside its own Error hierarchy while
    binding - it used to escape raw, with hooks/observers seeing exception=None."""
    calls = []

    def hook(event):
        sql, exception = event.sql, event.error
        calls.append((sql, exception))

    connection = db.get_connection()
    Observers.observe(QueryExecuted, hook)
    try:
        with pytest.raises(OperationalError, match="too large") as exc_info:
            await connection.execute("SELECT ?", [2**70])
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, hook)

    assert isinstance(exc_info.value.__cause__, OverflowError)
    assert exc_info.value.sql == "SELECT ?"
    failed_calls = [exception for sql, exception in calls if sql == "SELECT ?"]
    assert len(failed_calls) == 1
    assert failed_calls[0] is exc_info.value


@pytest.mark.asyncio
async def test_translate_exceptions_reports_an_untranslated_exception_to_hooks():
    calls = []

    def hook(event):
        exception = event.error
        calls.append(exception)

    class UnexpectedFailure(Exception):
        pass

    @AiosqliteClient.translate_exceptions
    async def execute(self, query, values=None):
        raise UnexpectedFailure("boom")

    Observers.observe(QueryExecuted, hook)
    try:
        with pytest.raises(UnexpectedFailure):
            await execute(
                SimpleNamespace(
                    connection_alias="default",
                    is_transaction_client=False,
                    driver_errors=AiosqliteClient.driver_errors,
                    _get_operational_error=AiosqliteClient._get_operational_error,
                    has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
                ),
                "SELECT 1",
            )
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, hook)

    assert len(calls) == 1
    assert isinstance(calls[0], UnexpectedFailure)


@pytest.mark.asyncio
async def test_translate_exceptions_works_for_commit_and_rollback_with_no_query_argument():
    """commit()/rollback() take no (query, values) arguments at all - the decorator's wrapper
    used to require a positional `query` parameter (`async def translate_exceptions_(self, query,
    *args, **kwargs)`), so wrapping either of these with @translate_exceptions crashed with a
    TypeError (missing required positional argument 'query') before this fix brought the
    signature in line with hare.dialects.postgresql.client's own `*args, **kwargs` one. Also
    proves a driver exception raised from either is now translated into
    hare.exceptions.IntegrityError, not left as a raw sqlite3.IntegrityError - previously left as
    a known gap (SqliteTransactionClient.commit()/rollback() were never wrapped at all)."""

    @AiosqliteClient.translate_exceptions
    async def commit(self):
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    with pytest.raises(IntegrityError):
        await commit(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            )
        )

    @AiosqliteClient.translate_exceptions
    async def rollback(self):
        return "ok"

    assert (
        await rollback(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            )
        )
        == "ok"
    )


@pytest.mark.asyncio
async def test_translate_exceptions_does_not_attach_sql_for_non_query_methods():
    """A function whose name isn't in QUERY_EXECUTING_METHOD_NAMES (e.g.
    create_connection) doesn't have a (sql, params) signature at all - its first
    positional arg after self shouldn't be misreported as sql."""

    @AiosqliteClient.translate_exceptions
    async def create_connection(self, with_db):
        raise sqlite3.OperationalError("boom")

    with pytest.raises(OperationalError) as exc_info:
        await create_connection(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            ),
            True,
        )

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

        @AiosqliteClient.translate_exceptions
        async def execute(self, query, values=None):
            if query.startswith("FAIL"):
                raise sqlite3.OperationalError("boom")
            return "ok"

        assert (
            await execute(
                SimpleNamespace(
                    connection_alias="default",
                    is_transaction_client=False,
                    driver_errors=AiosqliteClient.driver_errors,
                    _get_operational_error=AiosqliteClient._get_operational_error,
                    has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
                ),
                "SELECT 1",
            )
            == "ok"
        )
        # Hooks now dispatch in the background (never awaited by the query call itself) - drain
        # after EACH call, not just at the end, or the two calls' background tasks can finish in
        # either order and calls[0]/calls[1] below stop corresponding to call order.
        await Observers.wait_for_pending()

        with pytest.raises(OperationalError):
            await execute(
                SimpleNamespace(
                    connection_alias="default",
                    is_transaction_client=False,
                    driver_errors=AiosqliteClient.driver_errors,
                    _get_operational_error=AiosqliteClient._get_operational_error,
                    has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
                ),
                "FAIL SELECT",
                values=[1],
            )
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

    @AiosqliteClient.translate_exceptions
    async def execute(self, query, values=None):
        return "ok"

    with caplog.at_level("DEBUG", logger="hare.db_client"):
        await execute(
            SimpleNamespace(
                connection_alias="default",
                is_transaction_client=False,
                driver_errors=AiosqliteClient.driver_errors,
                _get_operational_error=AiosqliteClient._get_operational_error,
                has_returning_foreign_key_error_fault=AiosqliteClient.has_returning_foreign_key_error_fault,
            ),
            "SELECT 1",
        )

    assert any("Slow query" in message for message in caplog.messages)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_update_row_count_excludes_rows_changed_by_triggers(db):
    from hare import Connections
    from tests.testmodels import Tournament

    client = Connections.get("models")
    await Tournament.objects.create(name="a")
    await Tournament.objects.create(name="b")
    await client.execute_script(
        'CREATE TRIGGER "tournament_audit" AFTER UPDATE ON "tournament" BEGIN '
        'UPDATE "tournament" SET "desc" = \'audited\' WHERE "id" <> NEW."id"; END'
    )

    rows_affected, _ = await client.execute('UPDATE "tournament" SET "name" = \'x\' WHERE "name" = \'a\'')
    updated_count = await Tournament.objects.filter(name="b").update(name="y")

    assert rows_affected == 1
    assert updated_count == 1


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_execute_script_inside_a_transaction_stays_in_it(db_truncate):
    from tests.testmodels import Tournament

    with pytest.raises(ZeroDivisionError):
        async with Transactions.atomic() as connection:
            await connection.execute_script(
                'INSERT INTO "tournament" ("name", "created") VALUES (\'semi;colon\', \'2024-01-01\');'
                'INSERT INTO "tournament" ("name", "created") VALUES (\'second\', \'2024-01-01\')'
            )
            await Tournament.objects.create(name="after-script")
            assert await Tournament.objects.all().count() == 3
            raise ZeroDivisionError

    assert await Tournament.objects.all().count() == 0


async def _hold_transaction_open(entered: asyncio.Event, release: asyncio.Event) -> None:
    async with Transactions.atomic():
        await Tournament.objects.create(name="inside")
        entered.set()
        await release.wait()


@pytest.mark.asyncio
async def test_close_waits_for_an_open_transaction_in_another_task(tmp_path):
    """close() used to close the single connection out from under a transaction another task
    still had open - its COMMIT then failed with aiosqlite's raw "no active connection"."""
    async with HareContext() as ctx:
        await ctx.init(
            HareConfig.from_db_url(
                f"sqlite+aiosqlite://{tmp_path / 'close.sqlite3'}", {"models": ["tests.testmodels"]}
            )
        )
        await ctx.generate_schemas()
        client = ctx.connections.get("default")
        entered, release = asyncio.Event(), asyncio.Event()
        transaction_task = asyncio.create_task(_hold_transaction_open(entered, release))
        await asyncio.wait_for(entered.wait(), 5)

        close_task = asyncio.create_task(client.close())
        await asyncio.sleep(0.1)
        assert not close_task.done()
        release.set()
        await asyncio.wait_for(transaction_task, 5)
        await asyncio.wait_for(close_task, 5)

        assert client._connection is None
        assert await Tournament.objects.filter(name="inside").count() == 1


@pytest.mark.asyncio
async def test_close_from_inside_its_own_transaction_raises(tmp_path):
    async with HareContext() as ctx:
        await ctx.init(
            HareConfig.from_db_url(f"sqlite+aiosqlite://{tmp_path / 'own.sqlite3'}", {"models": ["tests.testmodels"]})
        )
        await ctx.generate_schemas()
        client = ctx.connections.get("default")
        async with Transactions.atomic():
            with pytest.raises(TransactionManagementError):
                await asyncio.wait_for(client.close(), 5)
            await Tournament.objects.create(name="still usable")
        assert await Tournament.objects.filter(name="still usable").count() == 1


@pytest.mark.asyncio
async def test_close_after_timeout_fails_the_abandoned_transaction_with_hare_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "SQLITE_CLOSE_TIMEOUT_SECONDS", 0.1)
    async with HareContext() as ctx:
        await ctx.init(
            HareConfig.from_db_url(
                f"sqlite+aiosqlite://{tmp_path / 'timeout.sqlite3'}", {"models": ["tests.testmodels"]}
            )
        )
        await ctx.generate_schemas()
        client = ctx.connections.get("default")
        entered, release = asyncio.Event(), asyncio.Event()
        transaction_task = asyncio.create_task(_hold_transaction_open(entered, release))
        await asyncio.wait_for(entered.wait(), 5)

        await asyncio.wait_for(client.close(), 5)
        release.set()
        with pytest.raises(DBConnectionError):
            await asyncio.wait_for(transaction_task, 5)
