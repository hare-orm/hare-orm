"""
Test some PostgreSQL-specific features
"""

import asyncio
import datetime
import ipaddress
import json
import os
import ssl
import subprocess
import sys
import textwrap
import time
import uuid
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from hare import Connections, Hare
from hare.contrib.test import requires_features
from hare.core.hare_context import HareContext
from hare.dialects.base.client import retryable_read_query_active
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.postgresql.fields.ranges import Range
from hare.exceptions import (
    ConfigurationError,
    DBConnectionError,
    IntegrityError,
    OperationalError,
    TransactionManagementError,
    UnSupportedError,
)
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.transactions.transactions import Transactions
from tests.testmodels import IntFields, Tournament
from tests.utils.database_under_test import DatabaseUnderTest


def _get_db_config():
    """Get database config and check which Postgres driver it resolves to."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    db_config = DbUrlConfigGenerator.build(
        db_url,
        app_modules={"models": ["tests.testmodels"]},
        connection_label="models",
        testing=True,
    )
    engine = db_config["connections"]["models"]["engine"]
    is_asyncpg = engine == "postgresql+asyncpg"
    is_psycopg = False
    is_rust_pg = engine == "postgresql"
    return db_config, is_asyncpg, is_psycopg, is_rust_pg


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_schema(db_isolated_no_schema):
    db_config, is_asyncpg, _, is_rust_pg = _get_db_config()

    InvalidSchemaNameError: type[BaseException]
    if is_rust_pg:
        # rust_pg has no granular per-SQLSTATE exception hierarchy the way asyncpg does (see
        # rust_pg/src/error.rs) - any non-integrity-violation query error, invalid-schema
        # included, becomes hare's own OperationalError via _translate_exceptions, so that's
        # what's actually raised here rather than a driver-specific "invalid schema" type.
        InvalidSchemaNameError = OperationalError
    elif is_asyncpg:
        # AsyncpgClient._translate_exceptions() used to have no catch-all fallback, so
        # InvalidSchemaNameError (not one of its individually-named except clauses) leaked as the
        # raw asyncpg exception here, unlike rust_pg above - now that it does (see that method's
        # own `except asyncpg.PostgresError` clause), asyncpg matches rust_pg's own
        # OperationalError for this exact scenario too, closing the cross-backend inconsistency.
        InvalidSchemaNameError = OperationalError
    else:
        from psycopg.errors import InvalidSchemaName

        InvalidSchemaNameError = InvalidSchemaName

    if Hare.is_inited():
        await Hare._drop_databases()

    try:
        db_config["connections"]["models"]["credentials"]["schema"] = "mytestschema"
        await Hare.init(db_config, _create_db=True)

        with pytest.raises(InvalidSchemaNameError):
            await Hare.generate_schemas()

        conn = Connections.get("models")
        await conn.execute_script("CREATE SCHEMA mytestschema;")
        await Hare.generate_schemas()

        tournament = await Tournament.objects.create(name="Test")
        await Connections.current().close_all()

        del db_config["connections"]["models"]["credentials"]["schema"]
        await Hare.init(db_config)

        with pytest.raises(OperationalError):
            await Tournament.objects.filter(name="Test").first()

        conn = Connections.get("models")
        _, res = await conn.execute("SELECT id, name FROM mytestschema.tournament WHERE name='Test' LIMIT 1")

        assert len(res) == 1
        assert tournament.id == res[0]["id"]
        assert tournament.name == res[0]["name"]
    finally:
        if Hare.is_inited():
            await Hare._drop_databases()


async def _drop_databases(ctx: HareContext) -> None:
    """Mirrors Hare._drop_databases(), scoped to an isolated, not-yet-torn-down context."""
    await ctx.connections.close_all(discard=False)
    for conn in ctx.connections.all():
        await conn.db_delete()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_ssl_true(db_isolated_no_schema):
    db_config, _, _, is_rust_pg = _get_db_config()

    if is_rust_pg:
        # rust_pg has no asyncpg-style bool/SSLContext "ssl" kwarg - "verify-full" (real
        # certificate + hostname verification) against a local Postgres with no matching cert
        # configured is its own equivalent forced-failure case.
        db_config["connections"]["models"]["credentials"]["ssl_mode"] = "verify-full"
    else:
        db_config["connections"]["models"]["credentials"]["ssl"] = True
    # An isolated HareContext, not Hare.init() - db_isolated_no_schema's own context is still the
    # ambient one (Hare.init() would reuse and re-init it in place via its contextvar lookup,
    # closing db_isolated_no_schema's real connection before this deliberately-broken one fails to
    # replace it, corrupting the state db_isolated_no_schema's own teardown relies on afterwards).
    try:
        async with HareContext() as ctx:
            await ctx.init(config=db_config, _create_db=True)
            # Connected despite ssl=True - clean up the database we just created before
            # asserting, since that's the failure path below and won't run cleanup itself.
            await _drop_databases(ctx)
            assert False, "Expected ConnectionError or SSLError"
    except (ConnectionError, ssl.SSLError):
        pass


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_ssl_custom(db_isolated_no_schema):
    db_config, _, _, is_rust_pg = _get_db_config()

    # Expect connectionerror or pass
    if is_rust_pg:
        # rust_pg has no asyncpg-style SSLContext "ssl" kwarg - "require" (encrypt, don't
        # verify the certificate) is its closest equivalent to a custom, verification-relaxed
        # SSL context.
        db_config["connections"]["models"]["credentials"]["ssl_mode"] = "require"
    else:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        db_config["connections"]["models"]["credentials"]["ssl"] = ssl_ctx
    # See test_ssl_true for why this uses an isolated HareContext rather than Hare.init().
    try:
        async with HareContext() as ctx:
            await ctx.init(config=db_config, _create_db=True)
            await _drop_databases(ctx)
    except ConnectionError:
        pass


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_ssl_mode_typo_raises_instead_of_silently_downgrading(db_isolated_no_schema):
    """rust.pg's connect() used to fall back silently to Prefer (encrypt opportunistically, but
    downgrade to plaintext the moment SSL negotiation fails for ANY reason) for ANY unrecognized
    ssl_mode string - a caller who typo'd "verify-full" as "verrify-full" thinking they'd
    required certificate verification got no verification and no encryption guarantee at all,
    with nothing to indicate their setting was ignored. Confirmed live: connect(...,
    ssl_mode="verrify-full") succeeded with no error raised whatsoever before this fix.
    asyncpg has no equivalent config knob at all (it uses ssl=<bool|SSLContext>, not a mode
    string) - this only applies to rust_pg."""
    db_config, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("ssl_mode is a rust_pg-only credential - asyncpg uses ssl=<bool|SSLContext>")

    db_config["connections"]["models"]["credentials"]["ssl_mode"] = "verrify-full"
    with pytest.raises(Exception, match="invalid ssl_mode"):
        async with HareContext() as ctx:
            await ctx.init(config=db_config, _create_db=True)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_application_name(db_isolated_no_schema):
    db_config, *_ = _get_db_config()

    db_config["connections"]["models"]["credentials"]["application_name"] = "mytest_application"
    try:
        await Hare.init(db_config, _create_db=True)

        conn = Connections.get("models")
        _, res = await conn.execute("SELECT application_name FROM pg_stat_activity WHERE pid = pg_backend_pid()")

        assert len(res) == 1
        assert "mytest_application" == res[0]["application_name"]
    finally:
        if Hare.is_inited():
            await Hare._drop_databases()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_server_settings_value_with_space_does_not_inject_extra_guc(db_isolated_no_schema):
    """rust.pg used to build its libpq `options` connection string as a raw, unescaped
    "-c key=value -c key=value ..." join - a server_settings VALUE containing a space smuggled
    in an entirely separate "-c" option instead of being treated as one atomic setting.
    Confirmed live: server_settings={"application_name": "legit -c statement_timeout=1"} set
    application_name to "legit" (truncated) AND statement_timeout to "1ms" on the actual server
    session - a value the caller never asked to set. asyncpg sends server_settings as
    structured startup-packet parameters (not a single command-line-style options string), so
    it was never exposed to this - this only applies to rust_pg."""
    db_config, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("this injection is specific to rust_pg's libpq options-string construction")

    db_config["connections"]["models"]["credentials"]["server_settings"] = {
        "application_name": "legit -c statement_timeout=1"
    }
    try:
        await Hare.init(db_config, _create_db=True)

        conn = Connections.get("models")
        _, app_name_rows = await conn.execute("SHOW application_name")
        _, timeout_rows = await conn.execute("SHOW statement_timeout")

        assert app_name_rows[0]["application_name"] == "legit -c statement_timeout=1"
        assert timeout_rows[0]["statement_timeout"] == "0"
    finally:
        if Hare.is_inited():
            await Hare._drop_databases()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_statement_cache_size_bounds_prepared_statements(db_isolated_no_schema):
    """rust.pg's `statement_cache_size` used to collapse into a bare enabled/disabled bool
    (rust/pg/src/client.rs) - the configured number itself was silently ignored, and
    `deadpool_postgres::StatementCache` (what actually backs the cache) has no eviction of its
    own, so every distinct SQL text a connection ever prepared stayed a live server-side prepared
    statement for that connection's whole lifetime. Confirmed live: with `statement_cache_size=5`
    and 20 distinct SELECTs pinned to a single connection (`max_size=1`), `pg_prepared_statements`
    grew to 21 before this fix. This only applies to rust_pg - asyncpg's own pool enforces its
    own `statement_cache_size` natively."""
    db_config, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("statement_cache_size bounding is rust_pg-specific")

    # The server's own prepared statements of this client's connection - past a transaction
    # pooler they would be the pooler's, shared by its clients.
    credentials = db_config["connections"]["models"]["credentials"]
    db_config["connections"]["models"]["credentials"] = DatabaseUnderTest.get_direct_credentials(credentials)
    db_config["connections"]["models"]["credentials"]["statement_cache_size"] = 5
    db_config["connections"]["models"]["credentials"]["max_size"] = 1
    db_config["connections"]["models"]["credentials"]["min_size"] = 1
    try:
        await Hare.init(db_config, _create_db=True)

        conn = Connections.get("models")
        for distinct_column_count in range(1, 21):
            columns = ", ".join(str(column) for column in range(distinct_column_count))
            await conn.execute(f"SELECT {columns}")

        _, rows = await conn.execute("SELECT count(*) AS n FROM pg_prepared_statements")
        # <= 6, not <= 5: the count query above is itself a 21st distinct statement that gets
        # prepared (and counted) before its own result comes back.
        assert rows[0]["n"] <= 6
    finally:
        if Hare.is_inited():
            await Hare._drop_databases()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_cancelled_query_frees_the_connection_instead_of_blocking_the_pool(db_isolated_no_schema):
    """rust.pg's ordinary query methods (Client::execute/fetch_all/fetch_one, rust/pg/src/
    client.rs) used to ignore Python-side cancellation entirely - `future_into_py()`'s own
    cancellation support only stops LOCAL polling of the Rust future, it never told the SERVER
    to abort a query already in flight. `asyncio.wait_for(..., timeout=X)` timing out therefore
    abandoned the query without actually stopping it: the pooled connection stayed busy running
    it to completion, and any LATER caller who drew that same connection back out of a small pool
    blocked behind it instead of getting a fresh one promptly. Fixed via a CancelQueryOnDrop
    guard around each of these methods, sending a real Postgres CancelRequest
    (`tokio_postgres::CancelToken::cancel_query`) if the guard is dropped before the query
    finished. max_size=1 forces the follow-up query to reuse the exact same connection."""
    db_config, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("this exact cancellation gap is rust_pg-specific (asyncpg cancels natively)")

    db_config["connections"]["models"]["credentials"]["max_size"] = 1
    db_config["connections"]["models"]["credentials"]["min_size"] = 1
    try:
        await Hare.init(db_config, _create_db=True)
        conn = Connections.get("models")

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(conn.execute("SELECT pg_sleep(2)"), timeout=0.2)

        start = time.monotonic()
        count, rows = await asyncio.wait_for(conn.execute("SELECT 42 AS answer"), timeout=1.0)
        elapsed = time.monotonic() - start
        assert rows[0]["answer"] == 42
        assert elapsed < 1.0, "a follow-up query on the same (size-1) pool must not wait for the cancelled query"
    finally:
        if Hare.is_inited():
            await Hare._drop_databases()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_cancelled_query_inside_a_transaction_aborts_it_promptly(db_isolated_no_schema):
    """Same underlying fix as test_rust_pg_cancelled_query_frees_the_connection_instead_of_
    blocking_the_pool above, applied to Transaction::execute/fetch_all/fetch_one (rust/pg/src/
    transaction.rs) - a transaction's own pinned connection is used by that ONE Transaction
    alone, so the abandoned-query symptom is a later statement on the SAME transaction blocking
    for however long the cancelled query would have taken to finish naturally, instead of
    Postgres's normal, immediate "current transaction is aborted" response a genuinely cancelled
    statement should produce."""
    db_config, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("this exact cancellation gap is rust_pg-specific (asyncpg cancels natively)")

    try:
        await Hare.init(db_config, _create_db=True)
        # Leaving the aborted transaction's block normally raises instead of committing.
        with pytest.raises(TransactionManagementError, match="instead of committed"):
            async with Transactions.atomic("models") as tx:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(tx.execute("SELECT pg_sleep(2)"), timeout=0.2)

                start = time.monotonic()
                with pytest.raises(TransactionManagementError, match="aborted"):
                    await asyncio.wait_for(tx.execute("SELECT 42 AS answer"), timeout=1.0)
                elapsed = time.monotonic() - start
                assert elapsed < 1.0, (
                    "the transaction must abort promptly, not hang until pg_sleep(2) would have finished"
                )
    finally:
        if Hare.is_inited():
            await Hare._drop_databases()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_stream_cancellation_does_not_deadlock_the_connection(db_isolated_no_schema):
    """rust_pg's `stream()` (`hare/dialects/postgresql/drivers/rust_pg/client.py`) used to run its
    row-fetch loop (`async for row in row_stream: yield row`) completely unshielded from Python-
    side cancellation - unlike every other transaction primitive on the same wrapper (`begin()`/
    `commit()`/`rollback()`/`savepoint()`, each wrapped in `_run_shielded_from_cancellation()` for
    exactly this reason). pyo3-async-runtimes cancels the underlying Rust future when the Python
    awaitable wrapping it is cancelled; `asyncio.wait_for(..., timeout=X)` timing out mid-fetch
    could therefore abort that Rust future in place, leaving something in the pyo3-async-
    runtimes<->tokio bridge wedged such that a LATER, already-shielded `rollback()`/`commit()` on
    the SAME transaction never received its own completion notification back - confirmed via
    `pg_stat_activity` on the stuck connection: `state=idle, wait_event=ClientRead,
    query='ROLLBACK'`, elapsed growing without bound, i.e. the ROLLBACK had already landed and
    gone idle server-side while the Python process hung on it forever. `py-spy dump` on the hung
    process showed the event loop idle in `_poll`/`select` (Windows IOCP), not CPU-busy - a real
    stuck wait, not a slow operation.

    Run in a genuinely separate process, with the assertion driven by `subprocess.run(...,
    timeout=...)` rather than `asyncio.wait_for()` inside THIS process: the bug this guards
    against wedges the event loop itself, which no in-process asyncio-level timeout can ever
    interrupt (that's exactly what made it a genuine, permanent deadlock rather than a slow
    operation). `subprocess.run`'s own OS-level timeout can still kill it, so a regression here
    fails this test promptly instead of hanging the whole suite - same reasoning as
    test_postgres_driver_dispatch.py's own subprocess-isolated test.
    """
    db_config, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("this exact cancellation gap is rust_pg-specific (asyncpg cancels natively)")

    script = textwrap.dedent(
        """
        import asyncio
        import os

        from hare import Hare
        from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
        from hare.transactions.transactions import Transactions
        from tests.testmodels import IntFields


        async def main() -> None:
            db_config = DbUrlConfigGenerator.build(
                os.environ["HARE_TEST_DB"],
                app_modules={"models": ["tests.testmodels"]},
                connection_label="models",
                testing=True,
            )
            # A single-connection pool forces every iteration below to reuse the SAME physical
            # connection, mirroring the original bug report's own pg_stat_activity trace (pinned
            # to one stuck connection) - a larger pool could mask a still-broken fetch by handing
            # a later iteration a fresh, uncorrupted connection instead of the poisoned one.
            db_config["connections"]["models"]["credentials"]["max_size"] = 1
            db_config["connections"]["models"]["credentials"]["min_size"] = 1

            await Hare.init(db_config, _create_db=True)
            try:
                await Hare.generate_schemas()
                for i in range(1, 51):
                    await IntFields.objects.create(intnum=i)

                async def consume() -> None:
                    async with Transactions.atomic():
                        async for _obj in IntFields.objects.all().order_by("id").stream(chunk_size=1):
                            pass

                # The literal timeout sequence from the original bug report (0.001s/0.002s/
                # 0.003s - the third call is what reproduced the permanent hang), repeated across
                # several cycles with a few extra tiny timeouts thrown in - the same "many
                # iterations, timeouts in the 0.1-10ms range" shape as the asyncpg-side stress
                # test this mirrors (30 iterations there, clean on every one).
                timeouts = [0.001, 0.002, 0.003, 0.0005, 0.004, 0.0015] * 5
                for timeout in timeouts:
                    try:
                        await asyncio.wait_for(consume(), timeout=timeout)
                    except (TimeoutError, asyncio.CancelledError):
                        pass

                # The real assertion: the pool connection must still be usable afterward - a
                # stuck one would make even this trivial query hang forever (pool maxsize=1,
                # nothing else to draw from), which is exactly the deadlock this guards against.
                assert await IntFields.objects.filter(intnum=1).exists()
                print("OK")
            finally:
                if Hare.is_inited():
                    await Hare._drop_databases()


        asyncio.run(main())
        """
    )
    repo_root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=repo_root,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            "rust_pg .stream() cancellation deadlocked the connection - the subprocess never "
            f"returned within 90s. stdout so far:\n{exc.stdout}\nstderr so far:\n{exc.stderr}"
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


@requires_features(dialect="postgresql")
@pytest.mark.parametrize("cancel_inside_the_stream", [False, True])
@pytest.mark.asyncio
async def test_cancelled_stream_does_not_block_the_transaction_rollback(db_isolated, cancel_inside_the_stream):
    """Cancelling a task mid-`.stream()` inside a transaction used to hang its ROLLBACK forever on
    rust_pg: the open portal's unread rows stop the connection from reading anything else, and
    the stream stayed open as long as the suspended generator - or, with the cancellation landing
    inside the stream, the kept exception's traceback - was alive. The pool connection leaked
    with it."""
    await IntFields.objects.bulk_create([IntFields(intnum=index) for index in range(3000)])
    kept_errors = []
    started = asyncio.Event()

    async def consume() -> None:
        async with Transactions.atomic():
            try:
                async for _instance in IntFields.objects.all().order_by("id").stream(chunk_size=5):
                    started.set()
                    if not cancel_inside_the_stream:
                        await asyncio.sleep(0)
            except asyncio.CancelledError as error:
                kept_errors.append(error)
                raise

    for _attempt in range(5):
        started.clear()
        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        done, _ = await asyncio.wait([task], timeout=20)
        assert done, "the transaction's ROLLBACK hung behind the cancelled stream"
        assert task.cancelled()

    assert len(kept_errors) == 5
    assert await asyncio.wait_for(IntFields.objects.filter(intnum=1).exists(), 20)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_transaction_end_closes_a_stream_left_open(db_isolated):
    """A stream still open when its transaction commits must not block the COMMIT, and reading
    from it afterwards fails instead of reading from a finished transaction."""
    await IntFields.objects.bulk_create([IntFields(intnum=index) for index in range(3000)])

    async def open_a_stream_and_commit():
        async with Transactions.atomic():
            stream = IntFields.objects.all().order_by("id").stream(chunk_size=5)
            first = await anext(stream)
        return stream, first

    stream, first = await asyncio.wait_for(open_a_stream_and_commit(), 20)
    try:
        assert first.intnum == 0
        with pytest.raises(TransactionManagementError):
            await asyncio.wait_for(anext(stream), 20)
    finally:
        await stream.aclose()
    assert await asyncio.wait_for(IntFields.objects.all().count(), 20) == 3000


def _get_query_plan(result: list):
    query_plan = result[0]["QUERY PLAN"]
    if isinstance(query_plan, str):
        query_plan = json.loads(query_plan)
    return query_plan[0]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain()
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_format_text(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(output_format="text")
    assert isinstance(result[0]["QUERY PLAN"], str)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_format_yaml(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(output_format="yaml")
    plan = yaml.safe_load(result[0]["QUERY PLAN"])
    assert plan[0]["Plan"]["Relation Name"] == "tournament"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_format_xml(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(output_format="xml")
    plan = ET.fromstring(result[0]["QUERY PLAN"])
    assert [element.text for element in plan.iter() if element.tag.endswith("Relation-Name")] == ["tournament"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_unsupported_format(db_simple):
    await Tournament.objects.create(name="Test")
    with pytest.raises(UnSupportedError) as exc_info:
        await Tournament.objects.all().explain(output_format="invalid")
    assert "Unsupported explain format" in str(exc_info.value)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_analyze(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(analyze=True)
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan
    assert "Actual Loops" in query_plan["Plan"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_costs(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(costs=True)
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan
    assert "Total Cost" in query_plan["Plan"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_buffers(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(buffers=True)
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan
    assert "Shared Hit Blocks" in query_plan["Plan"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_timing(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(analyze=True, timing=True)
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan
    assert "Actual Total Time" in query_plan["Plan"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_memory(db_simple):
    await Tournament.objects.create(name="Test")
    if "MEMORY" not in Tournament.get_connection().features.explain_options:
        pytest.skip("EXPLAIN (MEMORY) needs PostgreSQL 17")
    result = await Tournament.objects.all().explain(memory=True)
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan
    assert "Memory" in query_plan or "Memory" in str(query_plan)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_settings(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(settings=True)
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_summary(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(summary=True)
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan
    assert "Planning Time" in query_plan


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_multiple_options(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(analyze=True, costs=True, buffers=True)
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan
    assert "Actual Loops" in query_plan["Plan"]
    assert "Total Cost" in query_plan["Plan"]
    assert "Shared Hit Blocks" in query_plan["Plan"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_unsupported_option(db_simple):
    await Tournament.objects.create(name="Test")
    with pytest.raises(UnSupportedError) as exc_info:
        await Tournament.objects.all().explain(unsupported_option=True)
    assert "UNSUPPORTED_OPTION" in str(exc_info.value)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_option_false(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(analyze=False)
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan
    assert "Actual Loops" not in query_plan["Plan"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_default_verbose(db_simple):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain()
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan
    assert "Output" in query_plan["Plan"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_connection_loss_raises_db_connection_error(db_simple):
    """_translate_exceptions() must map a lost connection to hare's own DBConnectionError, the
    same way rust_pg's identical pg.ConnectionError -> DBConnectionError mapping already
    does (hare/backends/rust_pg/client.py) - this backend had no equivalent branch, so both a
    server-side PostgresConnectionError and a client-side InterfaceError (e.g. using a closed
    connection) used to propagate as the raw asyncpg exception type instead."""
    _, is_asyncpg, _, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg-specific _translate_exceptions mapping")

    import asyncpg

    conn = Connections.get("models")

    async def raise_interface_error(_self, *_args, **_kwargs):
        raise asyncpg.InterfaceError("connection is closed")

    with pytest.raises(DBConnectionError):
        await conn._translate_exceptions(raise_interface_error)

    async def raise_postgres_connection_error(_self, *_args, **_kwargs):
        raise asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed")

    with pytest.raises(DBConnectionError):
        await conn._translate_exceptions(raise_postgres_connection_error)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_begin_is_shielded_from_cancellation(db_simple):
    """AsyncpgClient.begin() used to send the real BEGIN/SAVEPOINT unshielded - a cancellation
    landing mid-network-round-trip could leave the connection genuinely open-and-uncommitted at
    the server while this wrapper's own bookkeeping never learned it happened, the same class of
    bug already fixed for rust_pg's own begin()/savepoint() (hare/backends/rust_pg/client.py).
    Confirms the wiring: begin() now genuinely routes through _run_shielded_from_cancellation,
    not just that the shared shielding mechanism itself works (already covered by
    tests/backends/test_transactional_client_shielding.py)."""
    _, is_asyncpg, _, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg-specific begin() shielding")

    from unittest import mock

    shielded_calls: list[object] = []
    real_shield = TransactionEnding.run_shielded_from_cancellation

    async def spying_shield(coroutine, **kwargs):
        shielded_calls.append(coroutine)
        return await real_shield(coroutine, **kwargs)

    with mock.patch.object(TransactionEnding, "run_shielded_from_cancellation", staticmethod(spying_shield)):
        async with Transactions.atomic():
            pass

    # commit() alone already accounts for 1 shielded call even before this fix - begin() must
    # add a SECOND one of its own, not just ride along on commit()'s pre-existing shielding.
    assert len(shielded_calls) >= 2, "begin() must route its real BEGIN through _run_shielded_from_cancellation too"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_query_cancel_deadlock_and_shutdown_exceptions_translated(db_simple):
    """_translate_exceptions() used to leave an entire class of failures unhandled, leaking as
    raw asyncpg.exceptions.* instead of any hare.exceptions.* type: QueryCanceledError (statement/
    lock timeout, or an explicit pg_cancel_backend), LockNotAvailableError (a NOWAIT lock
    acquisition failure), DeadlockDetectedError/SerializationError (retry-safe class-40 failures),
    and AdminShutdownError/CannotConnectNowError/TooManyConnectionsError (the connection itself is
    no longer usable, the same DBConnectionError semantics as PostgresConnectionError above).
    Confirmed live before this fix via a real `SET statement_timeout` + `pg_sleep()` against a
    real Postgres connection - QueryCanceledError reached the caller completely unwrapped.

    CrashShutdownError/DatabaseDroppedError (SQLSTATE 57P02/57P04, the other two of the same
    four-code OperatorInterventionError family AdminShutdownError/CannotConnectNowError belong
    to) were later found still falling through to the generic OperatorInterventionError ->
    OperationalError clause below instead of DBConnectionError - rust_pg's own error.rs already
    mapped all four codes uniformly, so a crashed backend or a dropped database surfaced as
    DBConnectionError there but OperationalError here, for the identical server condition.

    FeatureNotSupportedError (class 0A) was later found unhandled too - confirmed live via a
    real `GENERATED ALWAYS AS ((SELECT ...))` column, which Postgres itself rejects at DDL time;
    the raw asyncpg exception leaked instead of OperationalError.

    A full audit then found this method had no catch-all fallback AT ALL, unlike rust_pg's own
    error.rs (which maps any otherwise-unclassified Postgres error to a generic OperationalError)
    and unlike this same client's own stream() (which already had one) - confirmed
    live via CardinalityViolationError (a subquery returning >1 row), a realistic ORM-generated-
    SQL failure. Added `except asyncpg.PostgresError: raise OperationalError(exc)` as the final
    clause instead of naming the ~90 remaining subclasses individually - CardinalityViolationError
    below stands in for that whole class of previously-unhandled errors, not an exhaustive list."""
    _, is_asyncpg, _, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg-specific _translate_exceptions mapping")

    import asyncpg

    conn = Connections.get("models")

    async def make_raiser(exc: Exception):
        async def raiser(_self, *_args, **_kwargs):
            raise exc

        return raiser

    for exc_cls, expected in [
        (asyncpg.exceptions.QueryCanceledError, OperationalError),
        (asyncpg.exceptions.LockNotAvailableError, OperationalError),
        (asyncpg.exceptions.DeadlockDetectedError, OperationalError),
        (asyncpg.exceptions.SerializationError, OperationalError),
        (asyncpg.exceptions.AdminShutdownError, DBConnectionError),
        (asyncpg.exceptions.CrashShutdownError, DBConnectionError),
        (asyncpg.exceptions.CannotConnectNowError, DBConnectionError),
        (asyncpg.exceptions.DatabaseDroppedError, DBConnectionError),
        (asyncpg.exceptions.TooManyConnectionsError, DBConnectionError),
        # InternalServerError (class XX) and ObjectNotInPrerequisiteStateError/ObjectInUseError
        # (class 55, e.g. DROP DATABASE against a database another session still holds a
        # connection to - see db_delete() below) used to fall through this method entirely
        # untranslated, unlike every other operational failure above.
        (asyncpg.exceptions.InternalServerError, OperationalError),
        (asyncpg.exceptions.ObjectNotInPrerequisiteStateError, OperationalError),
        (asyncpg.exceptions.ObjectInUseError, OperationalError),
        # FeatureNotSupportedError (class 0A, e.g. a subquery inside a GENERATED ALWAYS AS
        # column expression, rejected by Postgres at DDL time) - confirmed live to leak as the
        # raw asyncpg exception instead of OperationalError, unlike the identical error on
        # sqlite (which already translates cleanly).
        (asyncpg.exceptions.FeatureNotSupportedError, OperationalError),
        # CardinalityViolationError (class 21, a subquery returning >1 row) - one representative
        # of the ~90 asyncpg.PostgresError subclasses the method's own catch-all now covers, not
        # named individually before this. Confirmed live via a real `SELECT (SELECT n FROM
        # (VALUES (1),(2)) t(n))` - the raw asyncpg exception leaked, while the identical failure
        # was already OperationalError on rust_pg (error.rs's own generic fallback).
        (asyncpg.exceptions.CardinalityViolationError, OperationalError),
    ]:
        raiser = await make_raiser(exc_cls("boom"))
        with pytest.raises(expected):
            await conn._translate_exceptions(raiser)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_wrong_password_raises_configuration_error_not_raw_exception(db_simple):
    """create_connection()'s own except clause had no mapping for InvalidAuthorizationSpecification
    Error (SQLSTATE 28000, e.g. InvalidPasswordError 28P01) at all - confirmed live against a
    real Postgres connection with a deliberately wrong password, the raw asyncpg exception
    leaked instead of any hare.exceptions.* type. ConfigurationError, not DBConnectionError -
    retrying (what code catching DBConnectionError typically does) can never fix a bad
    credential, so lumping it in with the transient-connection-loss cases would be misleading."""
    _, is_asyncpg, _, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg-specific create_connection mapping")

    shared = Connections.get("models")
    client = type(shared)(
        connection_alias="wrong_password_test",
        user=shared.user,
        password=f"{shared.password}-definitely-wrong",
        database=shared.database,
        host=shared.host,
        port=shared.port,
        transaction_pooling=shared.transaction_pooling,
        connect_max_retries=0,
    )
    with pytest.raises(ConfigurationError, match="Authentication failed"):
        await client.create_connection(with_db=True)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bad_pool_size_kwarg_raises_configuration_error_at_construction_time(db_simple):
    """pool_minsize=/pool_maxsize= are PostgresqlClient's own ATTRIBUTE names, not the real
    minsize=/maxsize= kwargs its __init__ actually expects - passing the typo used to behave
    asymmetrically between drivers: asyncpg.create_pool() rejected it with a raw, untranslated
    TypeError once create_connection() actually ran; rust_pg silently dropped it into self.extra
    with no error at all, leaving the pool at its unchanged defaults. Both are now caught eagerly
    at client construction time, before any connection attempt, on either driver."""
    shared = Connections.get("models")
    with pytest.raises(ConfigurationError, match="connection parameter"):
        type(shared)(
            connection_alias="bad_kwarg_test",
            user=shared.user,
            password=shared.password,
            database=shared.database,
            host=shared.host,
            port=shared.port,
            pool_minsize=2,
            pool_maxsize=2,
        )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_db_delete_with_active_connection_raises_operational_error(db_simple):
    """db_delete() (PostgresqlClient.db_delete -> DROP DATABASE) against a database another
    session still holds a connection to used to raise asyncpg's raw, untyped ObjectInUseError -
    confirmed live here against a real second connection rather than a mocked one, since the
    fix is a genuine SQLSTATE-class-55 mapping, not just an exception-translation unit test."""
    _, is_asyncpg, _, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg-specific _translate_exceptions mapping")

    import asyncpg

    shared = Connections.get("models")
    db_name = "hare_orm_db_delete_active_connection_test"

    admin = type(shared)(
        connection_alias="db_delete_active_connection_admin",
        user=shared.user,
        password=shared.password,
        database="postgres",
        host=shared.host,
        port=shared.port,
    )
    await admin.create_connection(with_db=True)
    try:
        await admin.execute_script(f'DROP DATABASE IF EXISTS "{db_name}"')
        await admin.execute_script(f'CREATE DATABASE "{db_name}" OWNER "{shared.user}"')

        other_session = await asyncpg.connect(
            host=shared.direct_host or shared.host,
            port=shared.direct_port or shared.port,
            user=shared.user,
            password=shared.password,
            database=db_name,
        )
        try:
            target = type(shared)(
                connection_alias="db_delete_active_connection_target",
                user=shared.user,
                password=shared.password,
                database=db_name,
                host=shared.host,
                port=shared.port,
            )
            with pytest.raises(OperationalError):
                await target.db_delete()
        finally:
            await other_session.close()
    finally:
        await admin.execute_script(f'DROP DATABASE IF EXISTS "{db_name}"')
        await admin.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_db_delete_of_an_already_missing_database_is_a_silent_no_op(db_simple):
    """db_delete()'s own `except asyncpg.InvalidCatalogNameError: pass` (making "delete a
    database that's already gone" an idempotent no-op) stopped matching once
    AsyncpgClient._translate_exceptions() grew a catch-all `except asyncpg.PostgresError`
    clause: InvalidCatalogNameError doesn't match any of that method's own individually-named
    exception classes, so the catch-all now wraps it into OperationalError BEFORE db_delete()'s
    own except clause ever sees the raw asyncpg type - confirmed live, DROP DATABASE against a
    nonexistent database raised OperationalError instead of being silently absorbed."""
    _, is_asyncpg, _, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg-specific db_delete()/_translate_exceptions interaction")

    shared = Connections.get("models")
    db_name = "hare_orm_db_delete_already_missing_test"
    admin = type(shared)(
        connection_alias="db_delete_already_missing_admin",
        user=shared.user,
        password=shared.password,
        database="postgres",
        host=shared.host,
        port=shared.port,
    )
    await admin.create_connection(with_db=True)
    try:
        await admin.execute_script(f'DROP DATABASE IF EXISTS "{db_name}"')
        target = type(shared)(
            connection_alias="db_delete_already_missing_target",
            user=shared.user,
            password=shared.password,
            database=db_name,
            host=shared.host,
            port=shared.port,
        )
        await target.db_delete()  # must not raise - the database never existed in the first place
    finally:
        await admin.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_db_create_closes_pool_on_create_database_failure(db_isolated_no_schema):
    """PostgresqlClient.db_create() (CREATE DATABASE) used to leave its freshly-opened
    connection pool open when the CREATE DATABASE call itself failed, unlike its sibling
    db_delete() (DROP DATABASE, see test above), which already wraps its own DDL call in
    try/finally: await self.close(). Exercised here against a real failure - CREATE DATABASE for
    a name that already exists, db_isolated_no_schema's own database, created by the fixture - rather than
    a mocked one. db_create() is a shared PostgresqlClient method neither AsyncpgClient nor
    RustPgClient overrides, so this isn't driver-scoped like most of the tests around it."""
    shared = Connections.get("models")
    target = type(shared)(
        connection_alias="db_create_already_exists_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.host,
        port=shared.port,
    )

    with pytest.raises(Exception):
        await target.db_create()

    assert target._pool is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_connect_retry_exhausted_raises_db_connection_error(db_simple):
    """create_connection() used to leak a raw ConnectionRefusedError/OSError once
    _create_pool_with_retry() exhausted its retries against an unreachable host, instead of
    hare's own DBConnectionError - unlike RustPgClient's identical scenario, already correctly
    mapped. Verified against a real unreachable port rather than a mocked create_pool()."""
    _, is_asyncpg, _, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg-specific create_connection retry-exhaustion mapping")

    shared = Connections.get("models")
    unreachable = type(shared)(
        connection_alias="connect_retry_exhausted_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.host,
        port=59999,
        connect_max_retries=1,
        connect_retry_backoff_base_seconds=0.01,
    )
    with pytest.raises(DBConnectionError):
        await unreachable.create_connection(with_db=True)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_mid_query_connection_loss_raises_db_connection_error(db_simple):
    """rust.pg's `From<tokio_postgres::Error>` (rust/pg/src/error.rs) used to only route a
    connection-acquisition failure (pool.get()) to DriverError::Connection -> DBConnectionError;
    a connection genuinely dropped MID-QUERY (server admin_shutdown/crash_shutdown/not-accepting-
    connections/database-dropped, or a raw socket/io failure with no structured SQLSTATE at all)
    fell through to the generic DriverError::Query -> OperationalError instead, unlike the
    identical failure already correctly classified on the asyncpg backend (see the
    test_asyncpg_connection_loss_raises_db_connection_error test above). No fake-exception
    injection point exists for the Rust layer (unlike _translate_exceptions' Python-level mock
    above) - this exercises the real classification end to end against a real killed connection.
    A plain query-cancel (statement_timeout, SQLSTATE 57014 - same "class 57" family as the admin-
    shutdown codes this fixes, but the connection itself stays perfectly usable) must NOT be
    reclassified as DBConnectionError by the same fix - checked here too so a future change to
    this SQLSTATE-code matching can't quietly widen it back to the whole class-57 prefix."""
    _, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("rust.pg-specific tokio_postgres::Error classification")

    conn = Connections.get("models")

    rows = await conn.execute_dicts("SELECT pg_backend_pid() AS pid")
    my_pid = rows[0]["pid"]

    async def killer():
        await asyncio.sleep(0.5)
        await conn.execute(f"SELECT pg_terminate_backend({my_pid})")

    async def victim():
        with pytest.raises(DBConnectionError):
            await conn.execute("SELECT pg_sleep(3)")

    await asyncio.gather(victim(), killer())


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_closing_the_pool_under_a_checked_out_connection_does_not_break_its_release(
    db_isolated_no_schema, monkeypatch
):
    import hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client as asyncpg_client_module

    # close() stops waiting for the busy connection and terminates the pool almost at once.
    monkeypatch.setattr(asyncpg_client_module, "POOL_CLOSE_TIMEOUT_SECONDS", 0.2)
    conn = db_isolated_no_schema.get_connection()
    await conn.execute("SELECT 1")

    async def busy_query() -> None:
        try:
            await conn.execute("SELECT pg_sleep(1)")
        except DBConnectionError:
            pass

    busy_task = asyncio.create_task(busy_query())
    await asyncio.sleep(0.1)
    await conn.close()
    await asyncio.wait_for(busy_task, timeout=10)

    assert await conn.execute_dicts("SELECT 1 AS ok") == [{"ok": 1}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_query_canceled_stays_operational_error(db_isolated_no_schema):
    """Companion to the mid-query-connection-loss test above - a cancelled-but-still-usable
    connection (SQLSTATE 57014, same "class 57" family as the admin-shutdown codes that test
    fixes) must stay OperationalError, not get swept into the DBConnectionError fix."""
    _, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("rust.pg-specific tokio_postgres::Error classification")

    conn = db_isolated_no_schema.get_connection()
    await conn.execute_script("SET statement_timeout = '200ms'")
    with pytest.raises(OperationalError) as exc_info:
        await conn.execute("SELECT pg_sleep(2)")
    assert not isinstance(exc_info.value, DBConnectionError)

    # The connection must still be usable afterward - a cancelled query is not a dead connection.
    rows = await conn.execute_dicts("SELECT 1 AS ok")
    assert rows == [{"ok": 1}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_invalid_transaction_state_raises_transaction_management_error(db_isolated_no_schema):
    """A statement run against a transaction/connection an earlier failed statement already
    poisoned (SQLSTATE class 25, e.g. 25P02 "current transaction is aborted, commands ignored
    until end of transaction block") must surface as hare's own TransactionManagementError on
    every Postgres driver. asyncpg already mapped this via its own
    asyncpg.InvalidTransactionStateError; rust_pg had no equivalent at all (rust/pg/src/error.rs
    had no SQLSTATE-class-25 branch, so this fell through to the generic
    DriverError::Query -> OperationalError instead) - confirmed live before the fix. Runs on
    both drivers - proves the existing asyncpg mapping still holds and the new rust_pg one now
    matches it."""
    with pytest.raises(TransactionManagementError):
        async with Transactions.atomic():
            conn = Connections.get("models")
            with pytest.raises(OperationalError):
                await conn.execute("SELECT 1/0")
            # The transaction is now poisoned server-side - this must raise
            # TransactionManagementError, not another generic OperationalError.
            await conn.execute("SELECT 1")

    # The connection isn't left corrupted - a subsequent transaction still works normally.
    rows = await Connections.get("models").execute_dicts("SELECT 1 AS ok")
    assert rows == [{"ok": 1}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_read_query_retries_and_succeeds_after_mid_query_connection_loss(db_simple):
    """Task: retry/backoff on a connection lost MID-READ. Same live scenario as
    test_rust_pg_mid_query_connection_loss_raises_db_connection_error above (pg_terminate_backend
    against a real pg_sleep() in flight) - proving DBConnectionError, not a retry, is what that
    test intentionally exercises with read_retry_max_retries left at its 0 default. Here,
    read_retry_max_retries is configured AND retryable_read_query_active is set (exactly what
    AwaitableQuery._execute_with_retry_context does for every real QuerySet/ValuesQuery/
    ValuesListQuery/CountQuery/ExistsQuery/AggregateQuery execution - simulated directly since the
    call under test is a raw execute(), to control precisely which statement gets killed):
    the SAME kill instead makes the query transparently retry against a fresh connection and
    succeed, on both asyncpg and rust_pg (the retry mechanism itself, hare.dialects.
    postgres_common.client, is shared by both - only the underlying DBConnectionError
    classification it depends on differs per driver, and that's already separately verified
    live)."""
    shared = Connections.get("models")
    # The kill below targets the backend of this client's own session - on the server itself.
    retryable = type(shared)(
        connection_alias="read_retry_success_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.direct_host or shared.host,
        port=shared.direct_port or shared.port,
        read_retry_max_retries=2,
        read_retry_backoff_base_seconds=0.05,
    )
    await retryable.create_connection(with_db=True)
    try:
        rows = await retryable.execute_dicts("SELECT pg_backend_pid() AS pid")
        my_pid = rows[0]["pid"]

        async def killer():
            await asyncio.sleep(0.5)
            await retryable.execute(f"SELECT pg_terminate_backend({my_pid})")

        async def victim():
            token = retryable_read_query_active.set(True)
            try:
                return await retryable.execute("SELECT pg_sleep(1.5)")
            finally:
                retryable_read_query_active.reset(token)

        start = time.monotonic()
        results = await asyncio.gather(victim(), killer())
        elapsed = time.monotonic() - start

        row_count, _ = results[0]
        assert row_count == 1
        # The first attempt was killed ~0.5s in, then a short backoff, then a full second
        # pg_sleep(1.5) on the fresh connection - well over a single successful (un-retried)
        # 1.5s query, proving a real retry (not a lucky race where the kill simply missed) is
        # what actually happened.
        assert elapsed > 1.8
    finally:
        await retryable.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_read_query_retry_reports_each_attempt_to_instrumentation(db_simple):
    """Task: QueryInstrumentation must observe a retried read once per PHYSICAL ATTEMPT, not once
    for the whole retried call. Before this fix, translate_exceptions()'s own finally recorded a
    single entry for the entire retry loop below it - two real failed attempts (each killed live
    via pg_terminate_backend here, same mechanism as
    test_read_query_retries_and_succeeds_after_mid_query_connection_loss above) before a
    successful third were completely invisible to any registered hook: no failure record at all,
    and the lone "success" record's duration silently absorbed both backoff sleeps and both killed
    attempts. Killed TWICE in a row (read_retry_max_retries=2) to force 2 recorded failures before
    the recorded success, proving this isn't just "eventually 1 record shows up" but genuinely one
    record per attempt."""
    shared = Connections.get("models")
    retryable = type(shared)(
        connection_alias="read_retry_instrumentation_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.host,
        port=shared.port,
        read_retry_max_retries=2,
        read_retry_backoff_base_seconds=0.05,
    )
    await retryable.create_connection(with_db=True)

    victim_sql = "SELECT pg_sleep(1)"
    calls = []

    def hook(event):
        # QueryInstrumentation is process-wide, so this also sees killer's own pg_stat_activity
        # polling queries (and anything else running concurrently on `shared`) - filtered down to
        # just the victim's own repeated attempts here, which all share this exact SQL text.
        sql, duration_ms, exception = event.sql, event.duration_ms, event.error
        if sql == victim_sql:
            calls.append((duration_ms, exception))

    Observers.observe(QueryExecuted, hook)
    try:

        async def killer():
            # Same pg_stat_activity lookup as test_write_query_is_never_retried_after_mid_query_
            # connection_loss below - the retried victim reconnects to a NEW pooled connection on
            # each attempt, so its pid can't be known ahead of time and has to be found fresh
            # before every one of the 2 kills. A terminated backend can still show up as active for a
            # moment, so an already-killed pid is skipped rather than "killed" a second time.
            killed_pids: set[int] = set()
            for _ in range(2):
                pid = None
                for _ in range(200):
                    rows = await shared.execute_dicts(
                        "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                        "AND state = 'active' AND query LIKE '%pg_sleep(1)%' AND pid <> pg_backend_pid()"
                    )
                    live_pids = [row["pid"] for row in rows if row["pid"] not in killed_pids]
                    if live_pids:
                        pid = live_pids[0]
                        break
                    await asyncio.sleep(0.01)
                assert pid is not None, "victim's query never showed up in pg_stat_activity"
                killed_pids.add(pid)
                await shared.execute(f"SELECT pg_terminate_backend({pid})")

        async def victim():
            token = retryable_read_query_active.set(True)
            try:
                return await retryable.execute(victim_sql)
            finally:
                retryable_read_query_active.reset(token)

        start = time.monotonic()
        results = await asyncio.gather(victim(), killer())
        elapsed = time.monotonic() - start

        row_count, _ = results[0]
        assert row_count == 1

        # Hooks now dispatch in the background (never awaited by the query call itself) - drain
        # every in-flight one before asserting on their effect, or this is flaky.
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, hook)
        await retryable.close()

    # 2 failed attempts + 1 successful attempt - not the single folded-together record the
    # pre-fix code produced.
    assert len(calls) == 3

    for duration_ms, exception in calls[:2]:
        assert isinstance(exception, DBConnectionError)
        # A killed attempt's own honest duration is however long it took to get killed (well
        # under a second here) - it must not include the OTHER attempt's kill wait, the backoff
        # sleeps between attempts, or the final full pg_sleep(1) success. The pre-fix bug would
        # have reported a single record whose duration was close to the whole `elapsed` value
        # instead.
        assert duration_ms < elapsed * 1000 * 0.7

    success_duration_ms, success_exception = calls[2]
    assert success_exception is None
    # The successful attempt's own duration is close to its own pg_sleep(1) (~1000ms) - not the
    # full elapsed time including the 2 earlier killed attempts and their backoff sleeps.
    assert 900 <= success_duration_ms < elapsed * 1000


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_write_query_is_never_retried_after_mid_query_connection_loss(db_simple):
    """Companion to the read-retry test above: a write is never routed through
    AwaitableQuery._execute_with_retry_context() (BulkCreateQuery/BulkUpdateQuery/DeleteQuery/
    UpdateQuery all keep AwaitableQuery.is_read_only at its default False), so
    retryable_read_query_active is never set for it - simulated directly here by deliberately
    NOT setting it around a real UPDATE. Even with read_retry_max_retries configured on the
    client, a connection lost mid-write must still raise DBConnectionError immediately, exactly
    as before this change: retrying a write blindly (no idempotency key exists anywhere in this
    project) could duplicate its effect, so this must never silently succeed via a retry."""
    shared = Connections.get("models")
    retryable = type(shared)(
        connection_alias="write_retry_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.host,
        port=shared.port,
        read_retry_max_retries=2,
        read_retry_backoff_base_seconds=0.05,
    )
    await retryable.create_connection(with_db=True)
    try:

        async def killer():
            # Looked up via pg_stat_activity rather than a prior "SELECT pg_backend_pid()" call
            # on `retryable` - that would only be reliable with pool affinity between two
            # separate acquire/release calls, which asyncpg's pool (default maxsize=16) doesn't
            # guarantee. `datname = current_database()` scopes this to the test's own throwaway
            # database (each test file/worker already gets a uniquely-named one), so this can't
            # pick up an unrelated pg_sleep from a concurrently-running xdist worker.
            # `pid <> pg_backend_pid()` excludes this lookup query itself - its own query text
            # contains the literal "%pg_sleep(1.5)%" LIKE pattern, so without this it can match
            # (and then terminate) its own backend rather than victim's.
            pid = None
            for _ in range(100):
                rows = await shared.execute_dicts(
                    "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                    "AND state = 'active' AND query LIKE '%pg_sleep(1.5)%' AND pid <> pg_backend_pid()"
                )
                if rows:
                    pid = rows[0]["pid"]
                    break
                await asyncio.sleep(0.01)
            assert pid is not None, "victim's query never showed up in pg_stat_activity"
            await shared.execute(f"SELECT pg_terminate_backend({pid})")

        async def victim():
            # retryable_read_query_active is deliberately left unset here - mirrors how an
            # UpdateQuery/DeleteQuery execution never sets it: is_read_only/retry-scoping is
            # driven entirely by that contextvar, not by sniffing the SQL text, so a plain
            # unconditional SELECT exercises the exact same "write path" behavior a real write
            # would get. Deliberately NOT "UPDATE ... FROM (SELECT pg_sleep(...)) WHERE ..." -
            # tournament is empty in this fixture, and Postgres's nested-loop plan for that shape
            # never even evaluates the FROM-clause subquery when the driving table side (the
            # indexed WHERE lookup against 0 rows) produces zero rows, so the query returned
            # near-instantly and killer never caught it mid-flight (confirmed live via EXPLAIN).
            with pytest.raises(DBConnectionError):
                await retryable.execute("SELECT pg_sleep(1.5)")

        start = time.monotonic()
        await asyncio.gather(victim(), killer())
        elapsed = time.monotonic() - start

        # No retry/backoff cycle happened - the error surfaced right after the kill, not after an
        # extra full pg_sleep(1.5) round trip on a fresh connection.
        assert elapsed < 1.5
    finally:
        await retryable.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_begin_rejects_isolation_level_sql_injection(db_isolated_no_schema):
    """rust.pg's `Client.begin(isolation=...)` (rust/pg/src/client.rs -> Transaction::begin,
    rust/pg/src/transaction.rs) used to splice `isolation` straight into
    "BEGIN ISOLATION LEVEL {isolation}" with no validation at all - confirmed live (before this
    fix) that `client.begin(isolation="SERIALIZABLE; CREATE TABLE proof (x int); --")` actually
    created the injected table via batch_execute's multi-statement simple query protocol. No
    caller in hare's own Python codebase passes anything but None today, but Client.begin is a
    public PyO3 method reachable directly from Python - this is defense in depth, the same
    reasoning already applied to savepoint names (validate_identifier)."""
    _, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("rust.pg-specific Client.begin(isolation=...) API")

    conn = db_isolated_no_schema.get_connection()
    await conn._ensure_connection()
    raw_client = conn._connected_pool

    for level in ["READ COMMITTED", "repeatable read", "Serializable", "READ UNCOMMITTED"]:
        tx = await raw_client.begin(isolation=level)
        await tx.rollback()

    with pytest.raises(ValueError, match="invalid isolation level"):
        await raw_client.begin(isolation="SERIALIZABLE; DROP TABLE pg_class; --")

    rows = await conn.execute_dicts("SELECT to_regclass('pg_class') IS NOT NULL AS ok")
    assert rows == [{"ok": True}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_duplicate_column_by_name_access_matches_asyncpg(db_isolated_no_schema):
    """rust.pg's PgRow (rust/row_abi/src/lib.rs) resolves a by-name lookup on a duplicate column
    name (e.g. a join selecting two tables that both have an "id" column) to the LAST matching
    column - confirmed this is intentional, matching asyncpg.Record's own identical last-wins
    behavior for the exact same query shape (the one other Postgres driver hare supports, so the
    one comparison that actually matters for drop-in parity), NOT sqlite3.Row (a different
    driver PgRow is never used alongside, and which does the opposite: first-wins)."""
    _, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("rust.pg-specific PgRow duplicate-column behavior")

    conn = db_isolated_no_schema.get_connection()
    rows = await conn.execute_dicts("SELECT 1 AS id, 2 AS id")
    assert rows == [{"id": 2}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_row_is_actually_iterable(db_isolated_no_schema):
    """PgRow (rust/row_abi/src/lib.rs) implemented __iter__ by returning the values LIST itself,
    not an iterator over it - Python's iter() protocol requires whatever __iter__ returns to
    itself support __next__ (a plain list only supports __iter__, same as any other
    iterable-but-not-iterator). Confirmed live: list(row)/for v in row/tuple(row) all raised
    "TypeError: iter() returned non-iterator of type 'list'" - PgRow was never actually iterable
    despite implementing __iter__. asyncpg.Record has no such gap (real tests never caught this
    because they always went through .to_dict()/dict access, never raw iteration)."""
    _, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("this is a rust.pg-specific PgRow bug, not a general driver-parity check")

    conn = db_isolated_no_schema.get_connection()
    _, rows = await conn.execute("SELECT 1 AS a, 2 AS b")
    row = rows[0]
    assert list(row) == [1, 2]
    assert tuple(row) == (1, 2)
    assert list(iter(row)) == [1, 2]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_exotic_types_decode_correctly_instead_of_raw_garbage(db_isolated_no_schema):
    """rust.pg's value decoder had no dedicated arm for MONEY/INTERVAL/CIDR/INET/MACADDR/
    BIT/VARBIT/_bytea[] - all fell through to the generic unknown-type fallback (AnyAsText),
    which either hex-encodes the raw binary wire bytes or (worse, for a type whose binary bytes
    happen to be valid UTF-8, e.g. MACADDR) returns them as a garbled string with embedded
    control characters. Confirmed live against asyncpg on the identical schema, which already
    decodes these correctly (Decimal/str/list[bytes]) - this only applies to rust_pg."""
    _, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("this is a rust.pg-specific decode gap, not a general driver-parity check")

    conn = db_isolated_no_schema.get_connection()
    await conn.execute_script(
        """
        CREATE TABLE exotic_types_probe (
            m MONEY, iv INTERVAL, c CIDR, i INET, mac MACADDR, b BIT(5), vb VARBIT,
            m_arr MONEY[], b_arr BYTEA[]
        )
        """
    )
    await conn.execute(
        r"""
        INSERT INTO exotic_types_probe VALUES (
            12.34, INTERVAL '1 year 2 months 3 days 04:05:06.789', '192.168.1.0/24',
            '10.0.0.5', '08:00:2b:01:02:03', B'10110', B'1101',
            ARRAY[1.11, 2.22]::money[], ARRAY['\x01020304'::bytea, '\xdeadbeef'::bytea]
        )
        """
    )
    _, rows = await conn.execute("SELECT m, iv, c, i, mac, b, vb, m_arr, b_arr FROM exotic_types_probe")
    row = rows[0]
    assert row["m"] == Decimal("12.34")
    assert row["iv"] == datetime.timedelta(days=365 + 60 + 3, hours=4, minutes=5, seconds=6, microseconds=789000)
    assert row["c"] == ipaddress.ip_network("192.168.1.0/24")
    assert row["i"] == ipaddress.ip_address("10.0.0.5")
    assert row["mac"] == "08:00:2b:01:02:03"
    assert row["b"] == "10110"
    assert row["vb"] == "1101"
    assert row["m_arr"] == [Decimal("1.11"), Decimal("2.22")]
    assert row["b_arr"] == [b"\x01\x02\x03\x04", b"\xde\xad\xbe\xef"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_range_arrays_decode_as_range_lists_instead_of_garbage(db_isolated_no_schema):
    """rust.pg's value decoder had a scalar-range decode arm (int4range/int8range/numrange/
    daterange/tsrange/tstzrange, via PgRangeCell) but no matching arm for the corresponding
    ARRAY-of-range OIDs (_int4range/_int8range/etc.) - those fell through to the generic
    unknown-type fallback (AnyAsText), which decodes the whole array as one hex/garbled blob
    instead of a list of Range values. Encoding (binding a Python list[Range] as a parameter)
    already worked correctly via the existing Value::Array -> per-element Value::Range dispatch -
    only this decode direction was missing. Confirmed live against asyncpg on the identical
    query, which already decodes these correctly - this only applies to rust_pg."""
    _, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("this is a rust.pg-specific decode gap, not a general driver-parity check")

    conn = db_isolated_no_schema.get_connection()
    _, rows = await conn.execute(
        "SELECT ARRAY[int4range(1,5,'[)'), int4range(10,20,'[)')] AS populated, "
        "ARRAY[]::int4range[] AS empty_arr, NULL::int4range[] AS null_arr"
    )
    row = rows[0]
    assert row["populated"] == [
        Range(lower=1, upper=5, lower_inc=True, upper_inc=False),
        Range(lower=10, upper=20, lower_inc=True, upper_inc=False),
    ]
    assert row["empty_arr"] == []
    assert row["null_arr"] is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_rejects_string_array_element_into_binary_typed_column(db_isolated_no_schema):
    """A Value::Text bound as a top-level scalar parameter correctly tells the wire protocol
    "this parameter is TEXT-format" so the server text-parses it via that type's own input
    function - but an ARRAY has no per-element format negotiation; every element inside a
    binary-encoded array is read as that member type's own BINARY layout, unconditionally.
    rust.pg used to just forward a string element's raw UTF-8 bytes there regardless, which
    either produced a confusing low-level Postgres wire error, or - when the string's byte
    length coincidentally matched the target's fixed binary width (e.g. 6 ASCII characters into
    MACADDR's 6-byte binary format) - silently wrote corrupted data with no error at all.
    Confirmed live: ["abcdef", "zyxwvu"] into a MACADDR[] column used to insert successfully,
    with Postgres's own `SELECT ... ::text` showing the ASCII bytes reinterpreted as a MAC
    address. asyncpg has no equivalent low-level bind-parameter API exposed this way - this
    only applies to rust_pg."""
    _, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("this is a rust.pg-specific bind-encoding gap, not a general driver-parity check")

    conn = db_isolated_no_schema.get_connection()
    await conn.execute_script("CREATE TABLE mac_array_probe (macs MACADDR[])")
    with pytest.raises(OperationalError):
        await conn.execute("INSERT INTO mac_array_probe (macs) VALUES ($1)", [["abcdef", "zyxwvu"]])

    # A plain scalar string bind against the same underlying type must still work - only the
    # array-element case is rejected.
    await conn.execute_script("CREATE TABLE mac_scalar_probe (mac MACADDR)")
    await conn.execute("INSERT INTO mac_scalar_probe (mac) VALUES ($1)", ["08:00:2b:01:02:03"])
    _, rows = await conn.execute("SELECT mac FROM mac_scalar_probe")
    assert rows[0]["mac"] == "08:00:2b:01:02:03"

    # A text array into a genuinely text-family column must still work fine.
    await conn.execute_script("CREATE TABLE text_array_probe (vals TEXT[])")
    await conn.execute("INSERT INTO text_array_probe (vals) VALUES ($1)", [["hello", "world"]])
    _, rows = await conn.execute("SELECT vals FROM text_array_probe")
    assert rows[0]["vals"] == ["hello", "world"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_pool_acquire_timeout_raises_db_connection_error(db_simple):
    """A caller waiting for a pool slot longer than pool_acquire_timeout gets a clear
    DBConnectionError instead of blocking forever - verified against a real, deliberately
    tiny (max_size=1) SEPARATE pool pointed at the same already-migrated test database, so this
    doesn't disturb the shared connection every other test in this module relies on. Covers both
    drivers: AsyncpgClient's native Pool.acquire(timeout=...) and RustPgClient's deadpool
    Timeouts baked in at pool construction (see PostgresqlClient._pool_acquire's own
    docstring for why the two need separate implementations)."""
    shared = Connections.get("models")
    small_pool_client = type(shared)(
        connection_alias="pool_acquire_timeout_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.host,
        port=shared.port,
        min_size=1,
        max_size=1,
        pool_acquire_timeout=0.3,
    )
    try:
        await small_pool_client.create_connection(with_db=True)

        async def hold_connection():
            async with small_pool_client.acquire_connection():
                await asyncio.sleep(1.0)

        async def try_second_acquire():
            await asyncio.sleep(0.05)
            with pytest.raises(DBConnectionError):
                async with small_pool_client.acquire_connection():
                    pass  # pragma: nocoverage - never reached, acquire() itself must raise

        await asyncio.gather(hold_connection(), try_second_acquire())
    finally:
        await small_pool_client.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_pool_acquire_timeout_none_waits_indefinitely(db_simple):
    """Backward compatibility: a client that never set pool_acquire_timeout (the default, None)
    still waits for a released connection rather than raising - the exact behavior every
    existing caller already depends on."""
    shared = Connections.get("models")
    small_pool_client = type(shared)(
        connection_alias="pool_acquire_timeout_default_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.host,
        port=shared.port,
        min_size=1,
        max_size=1,
    )
    assert small_pool_client.pool_acquire_timeout is None
    try:
        await small_pool_client.create_connection(with_db=True)

        async def hold_connection():
            async with small_pool_client.acquire_connection():
                await asyncio.sleep(0.5)

        async def acquire_after_wait():
            await asyncio.sleep(0.05)
            start = time.monotonic()
            async with small_pool_client.acquire_connection():
                assert time.monotonic() - start > 0.3

        await asyncio.gather(hold_connection(), acquire_after_wait())
    finally:
        await small_pool_client.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_transaction_finished_race_does_not_mask_the_real_exception(db_isolated_no_schema):
    """rust.pg's `Transaction.commit()`/`.rollback()` `.take()` the connection out of their
    Arc<Mutex<Option<Object>>> before the real COMMIT/ROLLBACK round-trip even starts (see
    rust/pg/src/transaction.rs) - so if the Python-side call awaiting that Rust future is
    cancelled (e.g. `asyncio.CancelledError` landing mid-commit), the underlying transaction can
    be genuinely finished while `RustPgTransactionClient._finalized`
    (hare/dialects/postgresql/drivers/rust_pg/client.py)
    never got updated to reflect it. Any LATER attempt on the same wrapper to commit/rollback
    again then hits the Rust side's "already committed or rolled back" error - which used to be
    a bare, un-namespaced `RuntimeError` that `__aexit__`'s defensive cleanup only caught as
    `pg.QueryError` (a different exception entirely), so it propagated and REPLACED whatever
    real exception (a `CancelledError`, or here a plain `ValueError` from application code) was
    actually in flight. Fixed two ways: the Rust side now raises a dedicated
    `pg.TransactionFinishedError` instead of a bare `RuntimeError`, and both
    `RustPgTransactionClient.commit()`/`.rollback()` (not just `__aexit__`'s own defensive rollback)
    now catch it and treat it as "already finished, nothing more to do" instead of letting it
    propagate.

    This test can't reliably force a REAL cancellation to land inside the Rust future's
    round-trip (that's a genuine race), so it simulates the state that race leaves behind
    directly: call the low-level `pg.Transaction.commit()` (bypassing the Python wrapper, which
    would otherwise correctly update `_finalized`), leaving `_finalized` stale (False) even
    though the underlying transaction is truly finished - exactly what an interrupted wrapper
    commit()/rollback() leaves behind."""
    _, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("rust.pg-specific RustPgTransactionClient/Transaction race")

    with pytest.raises(ValueError, match="boom - the real exception"):
        async with Transactions.atomic() as conn:
            await conn._native_transaction.commit()
            raise ValueError("boom - the real exception that should reach the caller")

    # The connection must still be usable afterward - the cleanup path didn't leave it corrupted.
    rows = await Connections.get("models").execute_dicts("SELECT 1 AS ok")
    assert rows == [{"ok": 1}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_raw_insert_returning_is_not_lost(db):
    """execute()'s write-statement dispatch only sniffed the leading keyword, not whether
    the statement also had a RETURNING clause - a raw INSERT/UPDATE/DELETE ... RETURNING ... took
    the status-string-only execute() path and silently discarded the actual returned rows."""
    result = await IntFields.objects.raw("INSERT INTO intfields (intnum) VALUES (42) RETURNING id, intnum")
    assert len(result) == 1
    assert result[0].intnum == 42


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_raw_multi_row_insert_reports_correct_affected_count(db):
    """asyncpg's status string is "UPDATE <count>"/"DELETE <count>" (2 tokens) but
    "INSERT <oid> <count>" (3 tokens, oid always 0 on modern Postgres) - reading a fixed index
    [1] instead of the last token always read the oid (0) for INSERT, never the real count."""
    db_client = HareContext.get_current().connections.get("models")
    rows_affected, _ = await db_client.execute("INSERT INTO intfields (intnum) VALUES (1), (2), (3)")
    assert rows_affected == 3


@pytest.mark.parametrize("inside_transaction", [False, True])
@pytest.mark.asyncio
async def test_rust_pg_abandoned_query_cancel_never_hits_the_next_callers_query(inside_transaction):
    """Bug: a query abandoned just as it finished sent its CancelRequest fire-and-forget while the
    connection went straight back to the pool - the cancel then landed on the NEXT caller's
    unrelated query on that connection ("canceling statement due to user request"). The
    abandoned query's connection is now discarded instead of reused."""
    raw_db_url = os.getenv("HARE_TEST_DB", "")
    if not raw_db_url.startswith("postgresql://"):
        pytest.skip("rust_pg's own drop-time cancel path")
    import random
    import uuid

    from hare.contrib.test.isolated_contexts import hare_test_context

    db_url = raw_db_url.replace("\\{", "{").replace("\\}", "}").format(uuid.uuid4().hex)
    db_url += ("&" if "?" in db_url else "?") + "min_size=1&max_size=1"
    async with hare_test_context(["tests.testmodels"], db_url=db_url, connection_label="models"):
        client = Connections.get("models")

        async def abandoned_query() -> None:
            if inside_transaction:
                async with Transactions.atomic() as transaction_client:
                    await transaction_client.execute("SELECT 1")
            else:
                await client.execute("SELECT 1")

        failures = []
        for _ in range(250):
            task = asyncio.create_task(abandoned_query())
            await asyncio.sleep(random.random() * 0.002)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            try:
                await client.execute("SELECT pg_sleep(0.002)")
            except Exception as error:
                failures.append(error)
        assert failures == []


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_list_bound_to_a_scalar_parameter_is_an_error_not_a_broken_connection(db):
    """rust_pg used to panic encoding a list against a non-array parameter type, which broke the
    transaction's connection - both drivers now raise OperationalError and stay usable."""
    db_client = HareContext.get_current().connections.get("models")
    with pytest.raises(OperationalError):
        async with Transactions.atomic() as connection:
            await connection.execute("SELECT $1::int AS v", [[1]])
    with pytest.raises(OperationalError):
        async with Transactions.atomic() as connection:
            await connection.execute("SELECT $1::timestamptz AS v", [["a"]])

    _, rows = await db_client.execute("SELECT $1::int AS v", [7])
    assert rows[0]["v"] == 7


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_list_bound_to_a_jsonb_parameter_is_a_json_array(db):
    if not os.getenv("HARE_TEST_DB", "").startswith("postgresql://"):
        pytest.skip("rust_pg binds a list to json/jsonb as a JSON array; asyncpg's codec only takes a str")
    db_client = HareContext.get_current().connections.get("models")

    _, rows = await db_client.execute("SELECT $1::jsonb AS v", [[1, "a", None, {"k": [2.5]}]])

    assert json.loads(rows[0]["v"]) == [1, "a", None, {"k": [2.5]}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_non_finite_numeric_and_infinite_dates_are_read_and_bound(db):
    """rust_pg failed the whole SELECT on NaN/Infinity numerics and infinite timestamps/dates -
    both drivers now return Decimal('NaN')/Decimal('Infinity') and the datetime/date extremes."""
    db_client = HareContext.get_current().connections.get("models")

    rows = await db_client.execute_dicts(
        "SELECT 'NaN'::numeric AS nan, 'Infinity'::numeric AS inf, '-Infinity'::numeric AS ninf, "
        "'infinity'::timestamptz AS ts_inf, '-infinity'::timestamp AS ts_ninf, "
        "'infinity'::date AS d_inf, '-infinity'::date AS d_ninf, "
        "ARRAY['NaN'::numeric, 1.5] AS numerics"
    )
    row = rows[0]

    assert row["nan"].is_nan()
    assert row["inf"] == Decimal("Infinity")
    assert row["ninf"] == Decimal("-Infinity")
    assert row["ts_inf"].replace(tzinfo=None) == datetime.datetime.max
    assert row["ts_ninf"] == datetime.datetime.min
    assert row["d_inf"] == datetime.date.max
    assert row["d_ninf"] == datetime.date.min
    assert row["numerics"][0].is_nan()
    assert row["numerics"][1] == Decimal("1.5")

    _, bound = await db_client.execute(
        "SELECT $1::numeric::text AS nan, $2::numeric::text AS ninf", [Decimal("NaN"), Decimal("-Infinity")]
    )
    assert (bound[0]["nan"], bound[0]["ninf"]) == ("NaN", "-Infinity")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_python_extremes_bind_back_as_infinity(db):
    """rust_pg wrote the date/datetime extremes a read of infinity decodes to back as finite
    dates - both drivers now bind them as infinity again, scalars, array elements and range
    bounds alike."""
    db_client = HareContext.get_current().connections.get("models")
    utc_min = datetime.datetime.min.replace(tzinfo=datetime.UTC)
    utc_max = datetime.datetime.max.replace(tzinfo=datetime.UTC)

    rows = await db_client.execute_dicts(
        "SELECT $1::date::text AS d_inf, $2::date::text AS d_ninf, $3::timestamp::text AS ts_inf, "
        "$4::timestamptz::text AS tstz_ninf, $5::timestamptz::text AS tstz_inf, $6::date[]::text AS dates, "
        "$7::tstzrange::text AS span, $8::daterange::text AS date_span",
        [
            datetime.date.max,
            datetime.date.min,
            datetime.datetime.max,
            utc_min,
            utc_max,
            [datetime.date.min, datetime.date(2026, 1, 1)],
            Range(utc_min, utc_max),
            Range(datetime.date(2026, 1, 1), datetime.date.max),
        ],
    )

    assert rows[0] == {
        "d_inf": "infinity",
        "d_ninf": "-infinity",
        "ts_inf": "infinity",
        "tstz_ninf": "-infinity",
        "tstz_inf": "infinity",
        "dates": "{-infinity,2026-01-01}",
        "span": "[-infinity,infinity)",
        "date_span": "[2026-01-01,infinity)",
    }


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_tsquery_reads_as_its_own_text_form(db):
    """rust_pg handed a tsquery (and a tsquery[] element) back as its raw binary wire bytes -
    both drivers now return the text tsqueryout prints."""
    db_client = HareContext.get_current().connections.get("models")
    queries = [
        "fat & cat",
        "fat & (cat | !dog)",
        "'it''s' & 'back\\slash'",
        "cat:*AB | dog:C",
        "a <-> (b <-> c)",
        "(a <-> b) <2> c",
        "!(a | b) & c",
        "a & !!b",
    ]
    for query in queries:
        rows = await db_client.execute_dicts(
            "SELECT to_tsquery('simple', $1) AS query, to_tsquery('simple', $1)::text AS text", [query]
        )
        assert rows[0]["query"] == rows[0]["text"], query

    rows = await db_client.execute_dicts(
        "SELECT plainto_tsquery('simple', '') AS empty, ARRAY[to_tsquery('simple', 'a & b'), NULL] AS queries"
    )
    assert rows[0] == {"empty": "", "queries": ["'a' & 'b'", None]}


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_list_of_strings_binds_to_an_array_of_a_non_text_type(db):
    """rust_pg refused (or corrupted) a list of strings bound to an array of a type whose binary
    format isn't its text - it now sends a text array literal the server parses element by
    element."""
    db_client = HareContext.get_current().connections.get("models")

    rows = await db_client.execute_dicts(
        "SELECT $1::macaddr[]::text AS addresses", [["08:00:2b:01:02:03", None, "08:00:2b:01:02:04"]]
    )

    assert rows[0]["addresses"] == "{08:00:2b:01:02:03,NULL,08:00:2b:01:02:04}"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_json_text_parameters_are_sent_verbatim(db_isolated_no_schema):
    """rust_pg parsed every string bound to json/jsonb through serde_json - floats came back
    changed, numbers beyond 64 bits lost precision and a json column's text was reordered. The
    text is now sent as-is on both drivers."""
    conn = db_isolated_no_schema.get_connection()
    await conn.execute_script("CREATE TABLE json_text_probe (id int, j json, jb jsonb)")
    floats = [-938371.9565467801, 0.1 + 0.2, 1e-300, 123456.78901234567]
    document = json.dumps({"f": floats})
    await conn.execute("INSERT INTO json_text_probe VALUES (1, $1, $2)", [document, document])
    rows = await conn.execute_dicts("SELECT j, jb FROM json_text_probe")
    assert json.loads(rows[0]["j"])["f"] == floats
    assert json.loads(rows[0]["jb"])["f"] == floats

    text = '{"b": 1, "a": 2, "f": 1.000}'
    await conn.execute("INSERT INTO json_text_probe (id, j) VALUES (2, $1)", [text])
    rows = await conn.execute_dicts("SELECT j::text AS j FROM json_text_probe WHERE id = 2")
    assert rows[0]["j"] == text

    rows = await conn.execute_dicts(
        "SELECT $1::jsonb::text AS v", ['{"n": 123456789012345678901234567890, "d": 0.1000000000000000000001}']
    )
    assert rows[0]["v"] == '{"d": 0.1000000000000000000001, "n": 123456789012345678901234567890}'


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_dict_parameter_keeps_key_order_and_exact_numbers(db_isolated_no_schema):
    if not _get_db_config()[3]:
        pytest.skip("asyncpg's json codec takes only a str")
    conn = db_isolated_no_schema.get_connection()
    value = {"z": 1, "a": [0.1, 2**70, None, True], "m": {"k": '\n"x"'}}
    rows = await conn.execute_dicts("SELECT $1::json::text AS v", [value])
    assert rows[0]["v"] == '{"z":1,"a":[0.1,1180591620717411303424,null,true],"m":{"k":"\\n\\"x\\""}}'


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_int_outside_int64_binds_to_numeric_and_float(db_isolated_no_schema):
    """rust_pg raised a bare builtins.OverflowError for any int outside int64 - even where the
    target type is numeric or float."""
    conn = db_isolated_no_schema.get_connection()
    rows = await conn.execute_dicts("SELECT $1::numeric AS n, $2::float8 AS f", [2**70, 2**70])
    assert rows[0] == {"n": Decimal(2**70), "f": float(2**70)}
    with pytest.raises(OperationalError):
        await conn.execute("SELECT $1::int8 AS v", [2**63])


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_value_of_the_wrong_type_is_rejected_not_reinterpreted(db_isolated_no_schema):
    """rust_pg wrote a value of the wrong type as its own raw binary bytes - bytes/date/datetime/
    time into integer arrays, a UUID into an interval array, a naive datetime into an int8 all
    silently became unrelated values. Both drivers now reject them; a naive datetime still binds
    to a date and a bool to an integer, as asyncpg allows."""
    conn = db_isolated_no_schema.get_connection()
    rejected = [
        ("SELECT $1::int4[] AS v", [[b"\x00\x00\x00\x07"]]),
        ("SELECT $1::int4[] AS v", [[datetime.date(2000, 1, 8)]]),
        ("SELECT $1::int8[] AS v", [[datetime.datetime(2000, 1, 1, 0, 0, 1)]]),
        ("SELECT $1::int8[] AS v", [[datetime.time(0, 0, 1)]]),
        ("SELECT $1::interval[]::text AS v", [[uuid.UUID(int=(1 << 64) + 5)]]),
        ("SELECT $1::int8 AS v", [datetime.datetime(2000, 1, 1, 0, 0, 1)]),
    ]
    for sql, params in rejected:
        with pytest.raises(OperationalError):
            await conn.execute(sql, params)
    rows = await conn.execute_dicts(
        "SELECT $1::int[] AS ints, $2::date AS day", [[True], datetime.datetime(2020, 1, 2, 3, 4)]
    )
    assert rows[0] == {"ints": [1], "day": datetime.date(2020, 1, 2)}


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_domain_and_oid_parameters(db_isolated_no_schema):
    """rust_pg could not bind an int to a domain or an oid parameter, and so reported a domain's
    CHECK violation as an OperationalError instead of an IntegrityError."""
    conn = db_isolated_no_schema.get_connection()
    await conn.execute_script(
        "CREATE DOMAIN positive_int AS integer CHECK (VALUE > 0); CREATE TABLE domain_probe (id int, p positive_int)"
    )
    await conn.execute("INSERT INTO domain_probe VALUES ($1, $2)", [1, 5])
    with pytest.raises(IntegrityError):
        await conn.execute("INSERT INTO domain_probe VALUES ($1, $2)", [2, -5])
    rows = await conn.execute_dicts(
        "SELECT $1::oid AS o, (SELECT relname FROM pg_class WHERE oid = $2) AS relname, "
        "ARRAY[5]::positive_int[] AS domain_array, p FROM domain_probe",
        [16384, 1259],
    )
    assert rows[0] == {"o": 16384, "relname": "pg_class", "domain_array": [5], "p": 5}


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_catalog_and_uncommon_types_decode_like_asyncpg(db_isolated_no_schema):
    """rust_pg decoded every type without a dedicated decoder by guessing from its binary bytes -
    oid/xid as control-character strings, regclass as a hex oid, int2vector as a blob. Values now
    match asyncpg: oid/xid as ints, reg* as names, vectors as lists, records as tuples."""
    conn = db_isolated_no_schema.get_connection()
    rows = await conn.execute_dicts(
        "SELECT 16384::oid AS oid, 'pg_class'::regclass AS regclass, 'int4'::regtype AS regtype, "
        "ARRAY['int4'::regtype] AS regtypes, '1'::xid AS xid, '1 2'::int2vector AS int2vector, "
        "'{16384}'::oid[] AS oids, '08:00:2b:01:02:03:04:05'::macaddr8 AS macaddr8, "
        "'{[1,3)}'::int4multirange AS multirange, ROW(1, 'a', ROW(2)) AS record, pg_sleep(0) AS void, "
        "'a'::\"char\" AS char"
    )
    row = rows[0]
    assert [(r.lower, r.upper, r.lower_inc, r.upper_inc) for r in row.pop("multirange")] == [(1, 3, True, False)]
    assert row == {
        "oid": 16384,
        "regclass": "pg_class",
        "regtype": "integer",
        "regtypes": ["integer"],
        "xid": 1,
        "int2vector": [1, 2],
        "oids": [16384],
        "macaddr8": "08:00:2b:01:02:03:04:05",
        "record": (1, "a", (2,)),
        "void": None,
        "char": b"a",
    }


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_types_without_a_decoder_read_as_their_text_form(db_isolated_no_schema):
    """A type rust_pg has no binary decoder for is read as the text its output function prints -
    in a plain query, inside a transaction and in a stream alike."""
    if not _get_db_config()[3]:
        pytest.skip("asyncpg has its own objects for these types")
    conn = db_isolated_no_schema.get_connection()
    query = (
        "SELECT '(1,2)'::point AS point, '0/16B3748'::pg_lsn AS lsn, '(1,2)'::tid AS tid, "
        "ARRAY['(3,4)'::point, NULL] AS points, ROW('(5,6)'::point) AS record FROM generate_series(1, 2)"
    )
    expected = {"point": "(1,2)", "lsn": "0/16B3748", "tid": "(1,2)", "points": ["(3,4)", None], "record": ("(5,6)",)}
    assert await conn.execute_dicts(query) == [expected, expected]
    async with Transactions.atomic() as transaction_client:
        assert await transaction_client.execute_dicts(query) == [expected, expected]
        streamed = [row.to_dict() async for row in transaction_client.stream(query)]
    assert streamed == [expected, expected]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_time_24_00_is_an_error_not_midnight(db_isolated_no_schema):
    """rust_pg read TIME/TIMETZ '24:00:00' as 00:00 - Python's time cannot hold it, so both drivers
    raise instead of returning a different time."""
    conn = db_isolated_no_schema.get_connection()
    for sql in ("SELECT time '24:00:00' AS v", "SELECT timetz '24:00:00+00' AS v"):
        with pytest.raises((ValueError, OperationalError)):
            await conn.execute(sql)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_server_error_carries_its_diagnostics(db_isolated_no_schema):
    """rust_pg's errors had no DETAIL, SQLSTATE or constraint name, and a failed parameter
    conversion said only "error serializing parameter N" without the reason."""
    conn = db_isolated_no_schema.get_connection()
    await conn.execute_script(
        "CREATE TABLE diagnostics_probe (name text CONSTRAINT diagnostics_probe_name_key UNIQUE)"
    )
    await conn.execute("INSERT INTO diagnostics_probe VALUES ('a')")
    with pytest.raises(IntegrityError) as raised:
        await conn.execute("INSERT INTO diagnostics_probe VALUES ('a')")
    assert "DETAIL:  Key (name)=(a) already exists." in str(raised.value)
    driver_error = raised.value.__cause__ or raised.value.__context__
    assert driver_error.sqlstate == "23505"
    assert driver_error.constraint_name == "diagnostics_probe_name_key"
    assert driver_error.table_name == "diagnostics_probe"
    assert driver_error.detail == "Key (name)=(a) already exists."

    with pytest.raises(OperationalError, match="out of (int32 )?range"):
        await conn.execute("SELECT $1::int4range AS v", [Range(1, 2**40)])


@requires_features(dialect="postgresql")
@pytest.mark.parametrize("inside_transaction", [False, True])
@pytest.mark.asyncio
async def test_connection_killed_by_the_server_is_not_reused(db_isolated_no_schema, inside_transaction):
    """rust_pg returned a connection the server had just terminated to its pool, so the next
    query on it failed with "connection closed" too."""
    conn = db_isolated_no_schema.get_connection()
    for _ in range(3):
        with pytest.raises(DBConnectionError):
            if inside_transaction:
                async with Transactions.atomic() as transaction_client:
                    await transaction_client.execute("SELECT pg_terminate_backend(pg_backend_pid())")
            else:
                await conn.execute("SELECT pg_terminate_backend(pg_backend_pid())")
        rows = await conn.execute_dicts("SELECT 1 AS v")
        assert rows == [{"v": 1}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_interval_and_network_values_match_asyncpg(db_isolated_no_schema):
    """rust_pg read interval as its own text and cidr without the prefix length - both drivers
    now return a timedelta (a month is 30 days, a year 365) and ipaddress objects, and accept both
    back as parameters."""
    conn = db_isolated_no_schema.get_connection()
    rows = await conn.execute_dicts(
        "SELECT interval '-1 days +02:00' AS a, interval '1 year 2 mons' AS b, "
        "interval '-13 mons -1 day -00:00:01' AS c, '10.0.0.1/32'::cidr AS cidr, "
        "'192.168.0.1/24'::inet AS interface, '10.0.0.5'::inet AS address, "
        "'2001:db8::/32'::cidr AS network_v6"
    )
    assert rows[0] == {
        "a": datetime.timedelta(days=-1, seconds=7200),
        "b": datetime.timedelta(days=425),
        "c": datetime.timedelta(days=-397, seconds=86399),
        "cidr": ipaddress.ip_network("10.0.0.1/32"),
        "interface": ipaddress.ip_interface("192.168.0.1/24"),
        "address": ipaddress.ip_address("10.0.0.5"),
        "network_v6": ipaddress.ip_network("2001:db8::/32"),
    }
    rows = await conn.execute_dicts(
        "SELECT $1::interval AS i, $2::inet AS n, $3::cidr[] AS networks",
        [
            datetime.timedelta(days=1, seconds=5),
            ipaddress.ip_interface("10.0.0.1/24"),
            [ipaddress.ip_network("10.0.0.0/8")],
        ],
    )
    assert rows[0] == {
        "i": datetime.timedelta(days=1, seconds=5),
        "n": ipaddress.ip_interface("10.0.0.1/24"),
        "networks": [ipaddress.ip_network("10.0.0.0/8")],
    }


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_list_of_ranges_binds_as_a_range_array(db_isolated_no_schema):
    """asyncpg's parameter adaptation converted a top-level Range but not the Range elements of a
    list, so an ArrayField(IntRangeField) value could not be written."""
    conn = db_isolated_no_schema.get_connection()
    rows = await conn.execute_dicts(
        "SELECT $1::int4range[]::text AS ranges, $2::int4range[]::text AS nested",
        [[Range(1, 5), Range(None, 3)], [[Range(1, 2)]]],
    )
    assert rows[0] == {"ranges": '{"[1,5)","(,3)"}', "nested": '{{"[1,2)"}}'}


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_db_delete_closes_the_clients_own_pool_first(db_isolated_no_schema):
    """db_delete() on a client whose pool is connected to the database closes that pool before
    dropping the database - the maintenance connection used to replace the pool without closing
    it, so DROP DATABASE failed with "being accessed by other users" and the pool leaked."""
    db_config, _, _, _ = _get_db_config()
    async with HareContext() as ctx:
        await ctx.init(db_config, _create_db=True)
        connection = ctx.connections.get("models")
        await connection.execute("SELECT 1")

        await connection.db_delete()

        await connection.create_connection(with_db=False)
        try:
            _, rows = await connection.execute("SELECT 1 FROM pg_database WHERE datname = $1", [connection.database])
        finally:
            await connection.close()
        assert rows == []


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_connection_error_names_its_cause(db_isolated_no_schema):
    """A failed connect says why - rust.pg kept only tokio-postgres's own "error connecting to
    server" and dropped its source() chain, the operating system's error ("connection refused")
    included."""
    _, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("the rust.pg error text is rust_pg-specific")
    from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient

    client = RustPgClient(
        host="127.0.0.1", port=1, user="postgres", password="postgres", database="unused", connection_alias="probe"
    )

    with pytest.raises(DBConnectionError, match=r"error connecting to server: .+\(os error \d+\)"):
        await client.create_connection(with_db=True)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_statement_without_cache_reads_rows_and_columns_in_one_round_trip(db_isolated_no_schema):
    """rust_pg caching no statements (``statement_cache_size=0``) sends a statement whose values all
    carry a type of their own (a UUID, a moment, NULL) with them in one round trip - the rows, the
    column names of a result of no row, and a write's count are the ones a prepared statement gives,
    outside a transaction and inside one."""
    db_config, _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("the one-round-trip statement is rust_pg's")
    credentials = db_config["connections"]["models"]["credentials"]
    db_config["connections"]["models"]["credentials"] = DatabaseUnderTest.get_direct_credentials(credentials)
    db_config["connections"]["models"]["credentials"]["statement_cache_size"] = 0
    try:
        await Hare.init(db_config, _create_db=True)
        conn = Connections.get("models")
        await conn.execute_script(
            "CREATE TABLE one_trip (id uuid PRIMARY KEY, at timestamptz, note text);"
            "INSERT INTO one_trip VALUES ('11111111-1111-1111-1111-111111111111', '2024-01-02T03:04:05Z', 'a')"
        )
        present = uuid.UUID("11111111-1111-1111-1111-111111111111")
        absent = uuid.UUID("22222222-2222-2222-2222-222222222222")
        moment = datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
        select_sql = "SELECT id, note FROM one_trip WHERE id = $1 AND at = $2 AND ($3::text IS NULL)"

        _, rows = await conn.execute(select_sql, [present, moment, None])
        assert [(row["id"], row["note"]) for row in rows] == [(present, "a")]
        described = await conn.execute_described(select_sql, [absent, moment, None])
        assert (list(described.columns), list(described.rows)) == (["id", "note"], [])
        count, _ = await conn.execute("UPDATE one_trip SET at = $1 WHERE id = $2", [moment, present])
        assert count == 1

        async with Transactions.atomic("models") as transaction:
            await transaction.execute("SELECT 1")
            _, rows = await transaction.execute(select_sql, [present, moment, None])
            assert [row["note"] for row in rows] == ["a"]
            described = await transaction.execute_described(select_sql, [absent, moment, None])
            assert list(described.columns) == ["id", "note"]
            count, _ = await transaction.execute("DELETE FROM one_trip WHERE id = $1", [present])
            assert count == 1
    finally:
        if Hare.is_inited():
            await Hare._drop_databases()
