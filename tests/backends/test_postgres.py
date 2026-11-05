"""
Test some PostgreSQL-specific features
"""

import asyncio
import datetime
import gc
import ipaddress
import json
import os
import ssl
import subprocess
import sys
import textwrap
import uuid
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.contrib.test.databases.test_database_config import TestDatabaseConfig as DatabaseConfig
from hare.core.hare_context import HareContext
from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.postgresql.fields.ranges import Range
from hare.exceptions import (
    ConfigurationError,
    DBConnectionError,
    IntegrityError,
    OperationalError,
    PoolTimeoutError,
    TransactionManagementError,
    UnSupportedError,
)
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import IntFields, Tournament
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.postgresql_query_cancellation import PostgresqlQueryCancellation
from tests.utils.server_session import ServerSession
from tests.utils.waiting import wait_until


def _get_db_config():
    """Get database config and check which Postgres driver it resolves to."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    db_config = DbUrlConfigGenerator.build(
        db_url,
        app_modules={"models": ["tests.shared_models.testmodels"]},
        connection_label="models",
    )
    db_config["connections"]["models"] = DatabaseConfig.from_url(db_url).config
    engine = db_config["connections"]["models"]["engine"]
    is_asyncpg = engine == "postgresql+asyncpg"
    is_rust_pg = engine == "postgresql"
    return db_config, is_asyncpg, is_rust_pg


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_schema(db_isolated_no_schema):
    """A configured schema receives the model tables and is absent from an unconfigured search path."""
    await DatabaseUnderTest.skip_if_the_pooler_loses_the_search_path()
    db_config, _, _ = _get_db_config()
    credentials = db_config["connections"]["models"]["credentials"]
    credentials["database"] = Connections.get("models").database
    credentials["schema"] = "mytestschema"

    async with HareContext() as context:
        await context.init(db_config)
        with pytest.raises(OperationalError):
            await context.generate_schemas()
        conn = Connections.get("models")
        await conn.execute_script("CREATE SCHEMA mytestschema")
        await context.generate_schemas()
        tournament = await Tournament.objects.create(name="Test")

    del credentials["schema"]
    async with HareContext() as context:
        await context.init(db_config)
        with pytest.raises(OperationalError):
            await Tournament.objects.filter(name="Test").first()
        rows = await Connections.get("models").execute_dicts(
            "SELECT id, name FROM mytestschema.tournament WHERE name='Test' LIMIT 1"
        )
        assert rows == [{"id": tournament.id, "name": tournament.name}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_required_tls_never_falls_back_to_plaintext(db_isolated_no_schema):
    """Required encryption succeeds with TLS or rejects a server that only offers plaintext."""
    shared = Connections.get("models")
    direct = {
        "host": shared.direct_host or shared.host,
        "port": shared.direct_port or shared.port,
        "transaction_pooling": False,
        "direct_host": None,
        "direct_port": None,
    }
    observer = Connections.current().create_independent("models", direct)
    _, _, is_rust_pg = _get_db_config()
    if is_rust_pg:
        tls_settings = {"ssl_mode": "require", "ssl_root_cert": None}
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        tls_settings = {"ssl": context, "ssl_root_cert": None}
    client = Connections.current().create_independent("models", {**direct, **tls_settings})
    try:
        [settings] = await observer.execute_dicts("SHOW ssl")
        if settings["ssl"] == "on":
            await client.create_connection(with_db=True)
            assert await client.execute_dicts("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()") == [
                {"ssl": True}
            ]
        else:
            with pytest.raises(DBConnectionError, match="(?i)ssl|tls"):
                await client.create_connection(with_db=True)
        assert await observer.execute_dicts("SELECT 1 AS value") == [{"value": 1}]
    finally:
        try:
            await client.close()
        finally:
            await observer.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_application_name(db_isolated_no_schema):
    conn = Connections.current().create_independent("models", {"application_name": "mytest_application"})
    try:
        await conn.create_connection(with_db=True)
        _, res = await conn.execute("SELECT application_name FROM pg_stat_activity WHERE pid = pg_backend_pid()")

        assert len(res) == 1
        assert "mytest_application" == res[0]["application_name"]
    finally:
        await conn.close()


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
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("this injection is specific to rust_pg's libpq options-string construction")

    shared = Connections.get("models")
    server_settings = {
        name: value
        for name, value in shared.server_settings.items()
        if name not in {"application_name", "statement_timeout"}
    }
    baseline = Connections.current().create_independent(
        "models", {"application_name": None, "server_settings": server_settings}
    )
    conn = Connections.current().create_independent(
        "models",
        {
            "application_name": None,
            "server_settings": {**server_settings, "application_name": "legit -c statement_timeout=1"},
        },
    )
    try:
        expected_timeout = await baseline.execute_dicts("SHOW statement_timeout")
        await conn.create_connection(with_db=True)
        _, app_name_rows = await conn.execute("SHOW application_name")
        timeout_rows = await conn.execute_dicts("SHOW statement_timeout")

        assert app_name_rows[0]["application_name"] == "legit -c statement_timeout=1"
        assert timeout_rows == expected_timeout
    finally:
        try:
            await conn.close()
        finally:
            await baseline.close()


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
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("statement_cache_size bounding is rust_pg-specific")

    # The server's own prepared statements of this client's connection - past a transaction
    # pooler they would be the pooler's, shared by its clients.
    shared = Connections.get("models")
    conn = Connections.current().create_independent(
        "models",
        {
            "host": shared.direct_host or shared.host,
            "port": shared.direct_port or shared.port,
            "transaction_pooling": False,
            "direct_host": None,
            "direct_port": None,
            "statement_cache_size": 5,
            "min_size": 1,
            "max_size": 1,
        },
    )
    try:
        await conn.create_connection(with_db=True)
        for distinct_column_count in range(1, 21):
            columns = ", ".join(str(column) for column in range(distinct_column_count))
            await conn.execute(f"SELECT {columns}")

        _, rows = await conn.execute("SELECT count(*) AS n FROM pg_prepared_statements")
        # <= 6, not <= 5: the count query above is itself a 21st distinct statement that gets
        # prepared (and counted) before its own result comes back.
        assert rows[0]["n"] <= 6
    finally:
        await conn.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_cancelled_query_frees_the_connection_instead_of_blocking_the_pool(db_isolated_no_schema):
    """rust.pg's ordinary query methods (Client::execute/fetch_all) used to ignore Python-side
    cancellation entirely - `future_into_py()`'s own
    cancellation support only stops LOCAL polling of the Rust future, it never told the SERVER
    to abort a query already in flight. `asyncio.wait_for(..., timeout=X)` timing out therefore
    abandoned the query without actually stopping it: the pooled connection stayed busy running
    it to completion, and any LATER caller who drew that same connection back out of a small pool
    blocked behind it instead of getting a fresh one promptly. Fixed via a CancelQueryOnDrop
    guard around each of these methods, sending a real Postgres CancelRequest
    (`tokio_postgres::CancelToken::cancel_query`) if the guard is dropped before the query
    finished. max_size=1 forces the follow-up query to reuse the exact same connection."""
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("this exact cancellation gap is rust_pg-specific (asyncpg cancels natively)")

    conn = Connections.current().create_independent("models", {"min_size": 1, "max_size": 1})
    try:
        await conn.create_connection(with_db=True)

        await PostgresqlQueryCancellation.cancel(conn.execute)

        # A query left running would hold the connection for its 30 seconds; a cancelled one frees
        # it at once - the bound leaves a loaded machine room for the cancel's round trips.
        count, rows = await asyncio.wait_for(conn.execute("SELECT 42 AS answer"), timeout=10.0)
        assert rows[0]["answer"] == 42
    finally:
        await conn.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_cancelled_query_on_an_acquired_connection_frees_it(db_isolated_no_schema):
    """A query on the raw connection acquire_connection() hands out is cancelled on the server as the
    pool's own queries are - the connection goes back to the pool idle, not still running it."""
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("rust_pg's own cancellation of a query on an acquired connection")

    conn = Connections.current().create_independent("models", {"min_size": 1, "max_size": 1})
    try:
        await conn.create_connection(with_db=True)

        async with conn.acquire_connection() as raw_connection:
            await PostgresqlQueryCancellation.cancel(lambda sql: raw_connection.execute(sql, []))

        count, rows = await asyncio.wait_for(conn.execute("SELECT 42 AS answer"), timeout=10.0)
        assert rows[0]["answer"] == 42
    finally:
        await conn.close()


@pytest.mark.requires("asyncpg")
@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_pool_failing_to_open_closes_a_connection_it_still_prepared(db_isolated_no_schema):
    """asyncpg puts a connection in its pool once the init callback prepared it - one still in init when
    the pool failed to open was missed by the pool's closing and stayed connected."""
    import asyncpg

    from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient

    db_config, _, is_rust_pg = _get_db_config()
    if is_rust_pg:
        pytest.skip("the asyncpg pool's own opening")
    credentials = db_config["connections"]["models"]["credentials"]
    application_name = f"hare_pool_init_{uuid.uuid4().hex[:8]}"
    connect_calls = 0
    init_calls = 0
    second_in_init = asyncio.Event()
    third_failed = asyncio.Event()

    async def connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 3:
            await second_in_init.wait()
            third_failed.set()
            raise asyncpg.exceptions.TooManyConnectionsError("sorry, too many clients already")
        return await asyncpg.connect(*args, **kwargs)

    async def slow_init(connection):
        # asyncpg opens a pool's first connection alone, the others together - the second is still
        # prepared when the third fails the pool.
        nonlocal init_calls
        init_calls += 1
        if init_calls == 2:
            second_in_init.set()
            await third_failed.wait()

    client = AsyncpgClient(
        connection_alias="pool_init",
        user=credentials["user"],
        password=credentials["password"],
        host=credentials["host"],
        port=credentials["port"],
        database="postgres",
        min_size=3,
        max_size=3,
        application_name=application_name,
        connect=connect,
        init=slow_init,
    )
    # The failed pool's own reference cycle closes the connection whenever the collector runs - the
    # test asks of hare's closing.
    gc.disable()
    try:
        with pytest.raises(DBConnectionError):
            await client.create_connection(with_db=True)
        await wait_until(lambda: _has_no_sessions(application_name))
    finally:
        gc.enable()


@pytest.mark.requires("asyncpg")
@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_pool_failing_to_open_closes_the_connections_it_opened(db_isolated_no_schema):
    """A pool whose min_size connections didn't all open (too many clients) left the ones it had
    opened - and the ones still opening - connected until the process ended."""
    import asyncpg

    from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient

    db_config, _, is_rust_pg = _get_db_config()
    if is_rust_pg:
        pytest.skip("the asyncpg pool's own opening")
    credentials = db_config["connections"]["models"]["credentials"]
    application_name = f"hare_pool_opening_{uuid.uuid4().hex[:8]}"
    connect_calls = 0
    third_failed = asyncio.Event()

    async def connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 3:
            # The others are opening together with this one.
            await wait_until(lambda: connect_calls == 5)
            third_failed.set()
            raise asyncpg.exceptions.TooManyConnectionsError("sorry, too many clients already")
        if connect_calls > 3:
            # Still opening when the pool fails.
            await third_failed.wait()
        return await asyncpg.connect(*args, **kwargs)

    client = AsyncpgClient(
        connection_alias="pool_opening",
        user=credentials["user"],
        password=credentials["password"],
        host=credentials["host"],
        port=credentials["port"],
        database="postgres",
        min_size=5,
        max_size=5,
        application_name=application_name,
        connect=connect,
    )
    with pytest.raises(DBConnectionError):
        await client.create_connection(with_db=True)

    # The connections still opening are closed once they open.
    await wait_until(lambda: _has_no_sessions(application_name))


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_cancelled_script_frees_the_connection_instead_of_blocking_the_pool(db_isolated_no_schema):
    """execute_script() (and execute_many(), copy_in()) ran outside the CancelQueryOnDrop guard - a
    cancelled script went on running on the server while its connection went back to the pool as
    idle, and the next caller drawing it waited for the script to end."""
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("asyncpg cancels a statement natively")

    conn = Connections.current().create_independent("models", {"min_size": 1, "max_size": 1})
    try:
        await conn.create_connection(with_db=True)

        await PostgresqlQueryCancellation.cancel(conn.execute_script)

        # A script left running would hold the connection for its 30 seconds; a cancelled one frees
        # it at once - the bound leaves a loaded machine room for the cancel's round trips.
        count, rows = await asyncio.wait_for(conn.execute("SELECT 42 AS answer"), timeout=10.0)
        assert rows[0]["answer"] == 42
    finally:
        await conn.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_cancelled_query_inside_a_transaction_aborts_it_promptly(db_isolated_no_schema):
    """Same underlying fix as test_rust_pg_cancelled_query_frees_the_connection_instead_of_
    blocking_the_pool above, applied to Transaction::execute/fetch_all - a transaction's own
    pinned connection is used by that ONE Transaction
    alone, so the abandoned-query symptom is a later statement on the SAME transaction blocking
    for however long the cancelled query would have taken to finish naturally, instead of
    Postgres's normal, immediate "current transaction is aborted" response a genuinely cancelled
    statement should produce."""
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("this exact cancellation gap is rust_pg-specific (asyncpg cancels natively)")

    conn = Connections.current().create_independent("models", {"min_size": 1, "max_size": 1})
    try:
        await conn.create_connection(with_db=True)
        # Leaving the aborted transaction's block normally raises instead of committing.
        with pytest.raises(TransactionManagementError, match="instead of committed"):
            async with conn._in_transaction() as tx:
                await PostgresqlQueryCancellation.cancel(tx.execute)

                # A query left running would hold the transaction for its 30 seconds; a cancelled
                # one aborts it at once.
                with pytest.raises(TransactionManagementError, match="aborted"):
                    await asyncio.wait_for(tx.execute("SELECT 42 AS answer"), timeout=10.0)
    finally:
        await conn.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_stream_cancellation_does_not_deadlock_the_connection(db_isolated_no_schema):
    """Cancel an active native fetch under an OS watchdog, then reuse the one-slot pool.

    The parent fixture owns the database even if the child's event loop becomes stuck.
    The server's activity report establishes the cancellation boundary.
    """
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("The native future cancellation bridge is specific to rust_pg")

    script = textwrap.dedent(
        """
        import asyncio
        import os

        from hare import Connections
        from hare.core.hare_context import HareContext
        from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
        from hare.transactions.transactions import Transactions
        from tests.utils.postgresql_query_cancellation import PostgresqlQueryCancellation


        async def main():
            connection = DbUrlConfigGenerator.expand(os.environ["HARE_TEST_DB"])
            connection["credentials"].update(
                database=os.environ["HARE_STREAM_TEST_DATABASE"], min_size=1, max_size=1
            )
            async with HareContext() as context:
                await context.init({"connections": {"models": connection}, "apps": {}})

                async def consume(sql):
                    async with Transactions.atomic("models") as transaction:
                        async for batch in transaction.stream_batches(sql, chunk_size=1):
                            pass

                await PostgresqlQueryCancellation.cancel(consume)
                assert await Connections.get("models").execute_dicts("SELECT 1 AS value") == [{"value": 1}]
                print("OK")


        asyncio.run(main())
        """
    )
    environment = {
        **os.environ,
        "HARE_STREAM_TEST_DATABASE": Connections.get("models").database,
    }
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(
            f"Stream cancellation left the native process blocked. stdout: {error.stdout}\nstderr: {error.stderr}"
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


@requires_features(dialect="postgresql")
@pytest.mark.parametrize("cancel_inside_the_stream", [False, True])
@pytest.mark.asyncio
async def test_cancelled_stream_does_not_block_the_transaction_rollback(db_isolated, cancel_inside_the_stream):
    """Rollback releases a stream even while a retained cancellation traceback keeps it alive."""
    kept_errors = []

    if cancel_inside_the_stream:

        async def consume(sql):
            async with Transactions.atomic() as transaction:
                try:
                    async for batch in transaction.stream_batches(sql, chunk_size=1):
                        pass
                except asyncio.CancelledError as error:
                    kept_errors.append(error)
                    raise

        await PostgresqlQueryCancellation.cancel(consume)
    else:
        await IntFields.objects.bulk_create([IntFields(intnum=index) for index in range(3)])
        started = asyncio.Event()
        release = asyncio.Event()

        async def consume():
            async with Transactions.atomic():
                try:
                    async for _instance in IntFields.objects.all().order_by("id").stream(chunk_size=1):
                        started.set()
                        await release.wait()
                except asyncio.CancelledError as error:
                    kept_errors.append(error)
                    raise

        task = asyncio.create_task(consume())
        try:
            async with asyncio.timeout(30):
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            if not task.done():
                task.cancel()
            await ResourceCompletion.wait(asyncio.gather(task, return_exceptions=True))

    assert len(kept_errors) == 1
    assert await asyncio.wait_for(Connections.get("models").execute_dicts("SELECT 1 AS value"), 30) == [{"value": 1}]


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


@pytest.mark.requires("yaml")
@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_format_yaml(db_simple):
    import yaml

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


@pytest.mark.parametrize(
    ("options", "message"),
    [
        pytest.param({"output_format": "invalid"}, "Unsupported explain format", id="explain_unsupported_format"),
        pytest.param({"unsupported_option": True}, "UNSUPPORTED_OPTION", id="explain_unsupported_option"),
    ],
)
@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explain_refuses_what_it_does_not_support(db_simple, options, message):
    await Tournament.objects.create(name="Test")
    with pytest.raises(UnSupportedError) as exc_info:
        await Tournament.objects.all().explain(**options)
    assert message in str(exc_info.value)


@pytest.mark.parametrize(
    ("option", "plan_key"),
    [
        pytest.param("analyze", "Actual Loops", id="explain_analyze"),
        pytest.param("costs", "Total Cost", id="explain_costs"),
        pytest.param("buffers", "Shared Hit Blocks", id="explain_buffers"),
    ],
)
@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_an_explain_option_shows_in_the_plan(db_simple, option, plan_key):
    await Tournament.objects.create(name="Test")
    result = await Tournament.objects.all().explain(**{option: True})
    query_plan = _get_query_plan(result)
    assert "Plan" in query_plan
    assert plan_key in query_plan["Plan"]


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
async def test_asyncpg_begin_is_shielded_from_cancellation(db_simple):
    """AsyncpgClient.begin() used to send the real BEGIN/SAVEPOINT unshielded - a cancellation
    landing mid-network-round-trip could leave the connection genuinely open-and-uncommitted at
    the server while this wrapper's own bookkeeping never learned it happened, the same class of
    bug already fixed for rust_pg's own begin()/savepoint() (hare/backends/rust_pg/client.py).
    Confirms the wiring: the real BEGIN - deferred until the transaction's first statement - is
    sent through run_shielded_from_cancellation, not just that the shared shielding mechanism
    itself works (already covered by tests/backends/test_transactional_client_shielding.py)."""
    _, is_asyncpg, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg-specific begin() shielding")

    from unittest import mock

    shielded_calls: list[object] = []
    real_shield = TransactionEnding.run_shielded_from_cancellation

    async def spying_shield(coroutine, **kwargs):
        shielded_calls.append(coroutine)
        return await real_shield(coroutine, **kwargs)

    with mock.patch.object(TransactionEnding, "run_shielded_from_cancellation", staticmethod(spying_shield)):
        async with Transactions.atomic() as connection:
            await connection.execute("SELECT 1")

    # commit() alone accounts for 1 shielded call - the BEGIN sent before the statement must add a
    # SECOND one of its own, not just ride along on commit()'s shielding.
    assert len(shielded_calls) >= 2, "the real BEGIN must be sent through run_shielded_from_cancellation too"


@pytest.mark.requires("asyncpg")
@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("exception_name", "expected"),
    [
        ("InterfaceError", DBConnectionError),
        ("ConnectionDoesNotExistError", DBConnectionError),
        ("QueryCanceledError", OperationalError),
        ("LockNotAvailableError", OperationalError),
        ("DeadlockDetectedError", OperationalError),
        ("SerializationError", OperationalError),
        ("AdminShutdownError", DBConnectionError),
        ("CrashShutdownError", DBConnectionError),
        ("CannotConnectNowError", DBConnectionError),
        ("DatabaseDroppedError", DBConnectionError),
        ("TooManyConnectionsError", DBConnectionError),
        ("InternalServerError", OperationalError),
        ("ObjectNotInPrerequisiteStateError", OperationalError),
        ("ObjectInUseError", OperationalError),
        ("FeatureNotSupportedError", OperationalError),
        ("CardinalityViolationError", OperationalError),
    ],
)
def test_asyncpg_maps_driver_errors(exception_name, expected):
    import asyncpg

    from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient

    client = AsyncpgClient(connection_alias="translation", user="test", password="test", database="test")
    error = getattr(asyncpg.exceptions, exception_name)("connection is closed")
    assert isinstance(client.get_driver_error(error, ("SELECT 1", [])), expected)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_wrong_password_raises_configuration_error_not_raw_exception(db_simple):
    """create_connection()'s own except clause had no mapping for InvalidAuthorizationSpecification
    Error (SQLSTATE 28000, e.g. InvalidPasswordError 28P01) at all - confirmed live against a
    real Postgres connection with a deliberately wrong password, the raw asyncpg exception
    leaked instead of any hare.exceptions.* type. ConfigurationError, not DBConnectionError -
    retrying (what code catching DBConnectionError typically does) can never fix a bad
    credential, so lumping it in with the transient-connection-loss cases would be misleading."""
    _, is_asyncpg, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg-specific create_connection mapping")

    shared = Connections.get("models")
    client = Connections.current().create_independent(
        "models",
        {
            "password": f"{shared.password}-definitely-wrong",
            "password_provider": None,
            "connect_max_retries": 0,
        },
    )
    try:
        with pytest.raises(ConfigurationError, match="Authentication failed"):
            await client.create_connection(with_db=True)
    finally:
        await client.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bad_pool_size_kwarg_raises_configuration_error_at_construction_time(db_simple):
    """pool_minsize=/pool_maxsize= are PostgresqlClient's own ATTRIBUTE names, not the real
    minsize=/maxsize= kwargs its __init__ actually expects - passing the typo used to behave
    asymmetrically between drivers: asyncpg.create_pool() rejected it with a raw, untranslated
    TypeError once create_connection() actually ran; rust_pg silently dropped it into self.extra
    with no error at all, leaving the pool at its unchanged defaults. Both are now caught eagerly
    at client construction time, before any connection attempt, on either driver."""
    with pytest.raises(ConfigurationError, match="connection parameter"):
        Connections.current().create_independent("models", {"pool_minsize": 2, "pool_maxsize": 2})


@pytest.mark.requires("asyncpg")
@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_db_delete_ends_the_sessions_still_on_the_test_database(db_simple):
    """Deleting a database terminates another client's session before dropping it."""
    _, is_asyncpg, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg database deletion")
    shared = Connections.get("models")
    db_name = f"hare_delete_{uuid.uuid4().hex}"
    admin = Connections.current().create_independent("models", {"database": "postgres"})
    target = Connections.current().create_independent("models", {"database": db_name})
    other = Connections.current().create_independent(
        "models",
        {
            "database": db_name,
            "host": shared.direct_host or shared.host,
            "port": shared.direct_port or shared.port,
            "transaction_pooling": False,
            "direct_host": None,
            "direct_port": None,
            "min_size": 1,
            "max_size": 1,
        },
    )
    try:
        await admin.create_connection(with_db=True)
        await admin.execute_script(f'CREATE DATABASE "{db_name}"')
        try:
            await other.create_connection(with_db=True)
            await target.db_delete()
            assert await admin.execute_dicts("SELECT 1 FROM pg_database WHERE datname = $1", [db_name]) == []
        finally:
            try:
                await other.close()
            finally:
                await target.db_delete()
    finally:
        try:
            await target.close()
        finally:
            await admin.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_db_delete_of_an_already_missing_database_is_a_silent_no_op(db_simple):
    """An absent database is already deleted; error translation must preserve that contract."""
    _, is_asyncpg, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg database deletion")
    target = Connections.current().create_independent("models", {"database": f"hare_missing_{uuid.uuid4().hex}"})
    try:
        await target.db_delete()
        assert target._pool is None
    finally:
        await target.close()


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
    target = Connections.current().create_independent("models")
    try:
        with pytest.raises(OperationalError):
            await target.db_create()
        assert target._pool is None
    finally:
        await target.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_connect_retry_exhausted_raises_db_connection_error(db_simple):
    """create_connection() used to leak a raw ConnectionRefusedError/OSError once
    _create_pool_with_retry() exhausted its retries against an unreachable host, instead of
    hare's own DBConnectionError - unlike RustPgClient's identical scenario, already correctly
    mapped. Verified against a real unreachable port rather than a mocked create_pool()."""
    _, is_asyncpg, _ = _get_db_config()
    if not is_asyncpg:
        pytest.skip("asyncpg-specific create_connection retry-exhaustion mapping")

    unreachable = Connections.current().create_independent(
        "models", {"host": "127.0.0.1", "port": 1, "connect_max_retries": 1, "connect_retry_backoff_base_seconds": 0}
    )
    try:
        with pytest.raises(DBConnectionError):
            await unreachable.create_connection(with_db=True)
    finally:
        await unreachable.close()


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
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("rust.pg-specific tokio_postgres::Error classification")

    conn = Connections.get("models")
    # The pool runs the query on any of its connections - the one running it is found by its text.
    victim_column = f"connection_loss_victim_{uuid.uuid4().hex}"

    async def terminated_the_victim() -> bool:
        rows = await conn.execute_dicts(
            "SELECT pg_terminate_backend(pid) AS terminated FROM pg_stat_activity "
            "WHERE state = 'active' AND query LIKE $1 AND pid <> pg_backend_pid()",
            [f"%{victim_column}%"],
        )
        return any(row["terminated"] for row in rows)

    async def killer():
        await wait_until(terminated_the_victim)

    async def victim():
        with pytest.raises(DBConnectionError):
            await conn.execute(f"SELECT pg_sleep(60) AS {victim_column}")

    await asyncio.gather(victim(), killer())


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_closing_the_pool_under_a_checked_out_connection_does_not_break_its_release(
    db_isolated_no_schema, monkeypatch
):
    # close() stops waiting for the busy connection and terminates the pool almost at once.
    client_class = DatabaseUnderTest.get_driver().get_client_class({})
    monkeypatch.setattr(sys.modules[client_class.__module__], "POOL_CLOSE_TIMEOUT_SECONDS", 0.2)
    conn = db_isolated_no_schema.get_connection()
    await conn.execute("SELECT 1")

    busy_query_sql = f"SELECT pg_sleep(1) AS busy_{uuid.uuid4().hex}"

    async def busy_query() -> None:
        try:
            await conn.execute(busy_query_sql)
        except DBConnectionError:
            pass

    busy_task = asyncio.create_task(busy_query())
    await wait_until(lambda: _is_running(conn, busy_query_sql))
    await conn.close()
    await asyncio.wait_for(busy_task, timeout=10)

    assert await conn.execute_dicts("SELECT 1 AS ok") == [{"ok": 1}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_query_canceled_stays_operational_error(db_isolated_no_schema):
    """Companion to the mid-query-connection-loss test above - a cancelled-but-still-usable
    connection (SQLSTATE 57014, same "class 57" family as the admin-shutdown codes that test
    fixes) must stay OperationalError, not get swept into the DBConnectionError fix."""
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("rust.pg-specific tokio_postgres::Error classification")

    conn = db_isolated_no_schema.get_connection()
    await conn.execute_script("SET statement_timeout = '200ms'")
    with pytest.raises(OperationalError) as exc_info:
        await conn.execute("SELECT pg_sleep(60)")
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
@pytest.mark.parametrize("failed_attempts", [1, 2])
@pytest.mark.asyncio
async def test_read_query_retry_reports_each_attempt_to_instrumentation(db_simple, failed_attempts):
    """Each confirmed connection loss is retried and reported before the successful attempt."""
    shared = Connections.get("models")
    retryable = Connections.current().create_independent(
        "models",
        {
            "host": shared.direct_host or shared.host,
            "port": shared.direct_port or shared.port,
            "transaction_pooling": False,
            "direct_host": None,
            "direct_port": None,
            "read_retry_max_retries": failed_attempts,
            "read_retry_backoff_base_seconds": 0,
        },
    )
    attempt_sequence = f"read_retry_attempt_{uuid.uuid4().hex}"
    await shared.execute_script(f"CREATE SEQUENCE {attempt_sequence}")
    victim_sql = f"SELECT pg_sleep(CASE WHEN nextval('{attempt_sequence}') <= {failed_attempts} THEN 60 ELSE 0 END)"
    errors = []
    killed_pids = set()

    def hook(event):
        if event.sql == victim_sql:
            errors.append(event.error)

    Observers.observe(QueryExecuted, hook)
    try:
        await retryable.create_connection(with_db=True)

        async def killer():
            for _ in range(failed_attempts):
                live_pids = []

                async def is_executing():
                    if victim.done():
                        await victim
                        pytest.fail("The read finished before its planned connection loss")
                    rows = await shared.execute_dicts(
                        "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                        "AND state = 'active' AND wait_event = 'PgSleep' "
                        "AND query = $1 AND pid <> pg_backend_pid()",
                        [victim_sql],
                    )
                    live_pids[:] = [row["pid"] for row in rows if row["pid"] not in killed_pids]
                    return len(live_pids) == 1

                await wait_until(is_executing)
                pid = live_pids[0]
                killed_pids.add(pid)
                assert await shared.execute_dicts("SELECT pg_terminate_backend($1) AS killed", [pid]) == [
                    {"killed": True}
                ]

        async with asyncio.timeout(60), asyncio.TaskGroup() as tasks:
            victim = tasks.create_task(retryable.execute(victim_sql, retry_read=True))
            tasks.create_task(killer())
        assert victim.result()[0] == 1
        assert len(killed_pids) == failed_attempts
        assert await shared.execute_dicts(f"SELECT last_value FROM {attempt_sequence}") == [
            {"last_value": failed_attempts + 1}
        ]
        await Observers.wait_for_pending()
        assert len(errors) == failed_attempts + 1
        assert all(isinstance(error, DBConnectionError) for error in errors[:-1])
        assert errors[-1] is None
    finally:
        Observers.unobserve(QueryExecuted, hook)
        try:
            await retryable.close()
        finally:
            await shared.execute_script(f"DROP SEQUENCE {attempt_sequence}")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_write_query_is_never_retried_after_mid_query_connection_loss(db_simple):
    """A real ORM UPDATE losing its server session fails without repeating the write.

    Another transaction holds the row lock. The server's lock-wait state is the barrier
    for termination; there are no query sleeps or elapsed-time assertions. Releasing the
    blocker lets an incorrect retry complete, so it fails the expected-error assertion.
    """
    shared = Connections.get("models")
    obj = await IntFields.objects.create(intnum=10)
    application_name = f"hare_write_retry_{uuid.uuid4().hex}"
    credentials = dict(DbUrlConfigGenerator.expand(DatabaseUnderTest.get_url())["credentials"])
    credentials.update(
        database=shared.database,
        application_name=application_name,
        read_retry_max_retries=2,
        read_retry_backoff_base_seconds=0,
    )
    retryable = type(shared)(connection_alias="write_retry_test", **credentials)
    attempts = []

    def observe(event):
        if event.connection_alias == retryable.connection_alias and event.sql.lstrip().upper().startswith("UPDATE"):
            attempts.append(event)

    async def write():
        with pytest.raises(DBConnectionError):
            await IntFields.objects.using(retryable).filter(pk=obj.pk).update(intnum=11)

    async def terminate_blocked_write():
        rows = await shared.execute_dicts(
            "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
            "AND application_name = $1 AND state = 'active' AND wait_event_type = 'Lock'",
            [application_name],
        )
        if not rows:
            return False
        [row] = rows
        await shared.execute("SELECT pg_terminate_backend($1)", [row["pid"]])
        return True

    Observers.observe(QueryExecuted, observe)
    try:
        await retryable.create_connection(with_db=True)
        async with asyncio.timeout(30):
            async with asyncio.TaskGroup() as tasks:
                async with shared._in_transaction() as blocker:
                    await IntFields.objects.using(blocker).filter(pk=obj.pk).select_for_update().get()
                    tasks.create_task(write())
                    await wait_until(terminate_blocked_write)
        await Observers.wait_for_pending()
        assert len(attempts) == 1
        assert isinstance(attempts[0].error, DBConnectionError)
        assert await IntFields.objects.filter(pk=obj.pk).values_list("intnum", flat=True) == [10]
    finally:
        Observers.unobserve(QueryExecuted, observe)
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
    _, _, is_rust_pg = _get_db_config()
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
    _, _, is_rust_pg = _get_db_config()
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
    _, _, is_rust_pg = _get_db_config()
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
    _, _, is_rust_pg = _get_db_config()
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
    _, _, is_rust_pg = _get_db_config()
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
    _, _, is_rust_pg = _get_db_config()
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
    """A held connection exhausts a one-slot pool; the next acquisition reports its timeout."""
    small_pool_client = Connections.current().create_independent(
        "models", {"min_size": 1, "max_size": 1, "pool_acquire_timeout": 0.3}
    )
    try:
        await small_pool_client.create_connection(with_db=True)

        async with asyncio.timeout(30):
            async with small_pool_client.acquire_connection():
                with pytest.raises(PoolTimeoutError):
                    async with small_pool_client.acquire_connection():
                        pytest.fail("An occupied one-slot pool admitted another connection")
    finally:
        await small_pool_client.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_pool_acquire_timeout_none_waits_indefinitely(db_simple):
    """Without an acquisition timeout, a queued query resumes when the held slot is released."""
    small_pool_client = Connections.current().create_independent(
        "models", {"min_size": 1, "max_size": 1, "pool_acquire_timeout": None}
    )
    assert small_pool_client.pool_acquire_timeout is None
    try:
        await small_pool_client.create_connection(with_db=True)

        async with asyncio.timeout(30), asyncio.TaskGroup() as tasks:
            async with small_pool_client.acquire_connection():
                waiting_query = tasks.create_task(small_pool_client.execute("SELECT 1 AS value"))
                await wait_until(lambda: small_pool_client.get_pool_status().waiting == 1)
                assert not waiting_query.done()
        assert [row["value"] for row in waiting_query.result()[1]] == [1]
    finally:
        await small_pool_client.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_transaction_finished_race_does_not_mask_the_real_exception(db_isolated_no_schema):
    """rust.pg's `Transaction.commit()`/`.rollback()` `.take()` the connection out of their
    Arc<Mutex<Option<Object>>> before the real COMMIT/ROLLBACK round-trip even starts (see
    rust/pg/src/transaction.rs) - so if the Python-side call awaiting that Rust future is
    cancelled (e.g. `asyncio.CancelledError` landing mid-commit), the underlying transaction can
    be genuinely finished while `RustPgTransactionClient.transaction_state.finished`
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
    _, _, is_rust_pg = _get_db_config()
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
@pytest.mark.timing
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
    async with hare_test_context(["tests.shared_models.testmodels"], db_url=db_url, connection_label="models"):
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
async def test_python_extremes_bind_as_their_dates_not_as_infinity(db):
    """Both drivers wrote the date/datetime extremes as infinity, which EXTRACT and date_trunc can't
    read (a year 9999 filter missed the row) - now they bind as the day and the moment they are,
    scalars, array elements and range bounds alike."""
    db_client = HareContext.get_current().connections.get("models")
    utc_min = datetime.datetime.min.replace(tzinfo=datetime.UTC)
    utc_max = datetime.datetime.max.replace(tzinfo=datetime.UTC)

    rows = await db_client.execute_dicts(
        "SELECT $1::date::text AS d_max, $2::date::text AS d_min, $3::timestamp::text AS ts_max, "
        "($4::timestamptz AT TIME ZONE 'UTC')::text AS tstz_min, "
        "($5::timestamptz AT TIME ZONE 'UTC')::text AS tstz_max, $6::date[]::text AS dates, "
        "lower($7::tstzrange) = $4::timestamptz AND upper($7::tstzrange) = $5::timestamptz AS span_bounds, "
        "$8::daterange::text AS date_span, EXTRACT(YEAR FROM $1::date)::int AS year",
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
        "d_max": "9999-12-31",
        "d_min": "0001-01-01",
        "ts_max": "9999-12-31 23:59:59.999999",
        "tstz_min": "0001-01-01 00:00:00",
        "tstz_max": "9999-12-31 23:59:59.999999",
        "dates": "{0001-01-01,2026-01-01}",
        "span_bounds": True,
        "date_span": "[2026-01-01,9999-12-31)",
        "year": 9999,
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
    if not _get_db_config()[2]:
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
    if not _get_db_config()[2]:
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
    async with TemporaryDatabases(
        {"connections": {"models": DatabaseUnderTest.get_url()}, "apps": {}},
        DatabaseUnderTest.get_url(),
        generate_schemas=False,
        reuse_databases=False,
    ) as ctx:
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
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("the rust.pg error text is rust_pg-specific")
    from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient

    client = RustPgClient(
        host="127.0.0.1", port=1, user="postgres", password="postgres", database="unused", connection_alias="probe"
    )

    with pytest.raises(DBConnectionError, match=r"error connecting to server: .+\(os error \d+\)"):
        await client.create_connection(with_db=True)


@pytest.mark.timing
@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_pooled_connection_left_unused_is_closed(db_isolated_no_schema):
    """An idle connection expires and the same client can open another for its next query."""
    connection = Connections.current().create_independent("models", {"max_inactive_connection_lifetime": 1})
    try:
        await connection.execute("SELECT 1")
        await wait_until(lambda: connection.get_pool_status().size == 0)
        assert await connection.execute_dicts("SELECT 1 AS one") == [{"one": 1}]
    finally:
        await connection.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_closing_the_pool_ends_a_transaction_still_open(db_isolated_no_schema, monkeypatch):
    """Forced pool closure ends the session of a held transaction before its holder is cancelled."""
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("the pool of rust_pg")
    from hare.dialects.postgresql.drivers.rust_pg.client import rust_pg_client

    monkeypatch.setattr(rust_pg_client, "POOL_CLOSE_TIMEOUT_SECONDS", 0.2)
    shared = Connections.get("models")
    connection = Connections.current().create_independent(
        "models",
        {
            "host": shared.direct_host or shared.host,
            "port": shared.direct_port or shared.port,
            "transaction_pooling": False,
            "direct_host": None,
            "direct_port": None,
        },
    )
    holding = asyncio.Event()
    release = asyncio.Event()
    pids = []
    holder = None

    async def hold_transaction():
        async with connection._in_transaction() as transaction:
            [row] = await transaction.execute_dicts("SELECT pg_backend_pid() AS pid")
            pids.append(row["pid"])
            holding.set()
            await release.wait()

    try:
        await connection.create_connection(with_db=True)
        holder = asyncio.create_task(hold_transaction())
        await asyncio.wait_for(holding.wait(), 30)
        await connection.close()

        async def has_no_session():
            return not await Connections.get("models").execute_dicts(
                "SELECT 1 FROM pg_stat_activity WHERE pid = $1", [pids[0]]
            )

        await wait_until(has_no_session)
    finally:
        try:
            if holder is not None:
                if not holder.done():
                    holder.cancel()
                await ResourceCompletion.wait(asyncio.gather(holder, return_exceptions=True))
        finally:
            await connection.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_statement_without_cache_reads_rows_and_columns_in_one_round_trip(db_isolated_no_schema):
    """rust_pg caching no statements (``statement_cache_size=0``) sends a statement whose values all
    carry a type of their own (a UUID, a moment, NULL) with them in one round trip - the rows, the
    column names of a result of no row, and a write's count are the ones a prepared statement gives,
    outside a transaction and inside one."""
    _, _, is_rust_pg = _get_db_config()
    if not is_rust_pg:
        pytest.skip("the one-round-trip statement is rust_pg's")
    shared = Connections.get("models")
    conn = Connections.current().create_independent(
        "models",
        {
            "host": shared.direct_host or shared.host,
            "port": shared.direct_port or shared.port,
            "transaction_pooling": False,
            "direct_host": None,
            "direct_port": None,
            "statement_cache_size": 0,
        },
    )
    try:
        await conn.create_connection(with_db=True)
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

        async with conn._in_transaction() as transaction:
            await transaction.execute("SELECT 1")
            _, rows = await transaction.execute(select_sql, [present, moment, None])
            assert [row["note"] for row in rows] == ["a"]
            described = await transaction.execute_described(select_sql, [absent, moment, None])
            assert list(described.columns) == ["id", "note"]
            count, _ = await transaction.execute("DELETE FROM one_trip WHERE id = $1", [present])
            assert count == 1
    finally:
        await conn.close()


def _make_pool_client(application_name: str, **options: Any) -> Any:
    """A client of the configured Postgres driver on the server's own database, its sessions told
    apart by ``application_name``."""
    db_config, _, _ = _get_db_config()
    credentials = DatabaseUnderTest.get_direct_credentials(db_config["connections"]["models"]["credentials"])
    client_class = DatabaseUnderTest.get_driver().get_client_class(credentials)
    credentials = {**credentials, "database": "postgres", "application_name": application_name, **options}
    return client_class(connection_alias="pool_closing", **credentials)


async def _has_no_sessions(application_name: str) -> bool:
    return await _count_sessions(application_name) == 0


async def _is_running(conn, query_start: str) -> bool:
    """Whether another server session runs a query beginning with a text."""
    running_sql = (
        "SELECT 1 AS running FROM pg_stat_activity "
        "WHERE state = 'active' AND query LIKE $1 AND pid <> pg_backend_pid()"
    )
    return bool(await conn.execute_dicts(running_sql, [query_start + "%"]))


async def _count_sessions(application_name: str) -> int:
    if DatabaseUnderTest.pools_server_sessions():
        pytest.skip(
            "a transaction pooler keeps its server sessions, with the client's application_name, after the "
            "client's connections close"
        )
    counter = await ServerSession.open(DatabaseUnderTest.get_url(), "postgres")
    try:
        return await counter.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = $1", application_name
        )
    finally:
        await counter.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_pool_opening_while_its_client_closes_is_closed_too(db_isolated_no_schema, monkeypatch):
    """close() while the pool was still opening found no pool to close; the pool then opened on the
    closed client and stayed connected until the process ended (rust_pg also raised a raw KeyError -
    the closed client's settings were cleared). It is closed, and the next use opens a new one."""
    application_name = f"hare_pool_closing_{uuid.uuid4().hex[:8]}"
    client = _make_pool_client(application_name, min_size=3, max_size=5)
    original_create_pool = client.create_pool
    pool_created = asyncio.Event()
    pool_released = asyncio.Event()

    async def create_pool_slowly(**kwargs: Any) -> Any:
        pool = await original_create_pool(**kwargs)
        pool_created.set()
        # The pool is handed over only once the client closed.
        await pool_released.wait()
        return pool

    monkeypatch.setattr(client, "create_pool", create_pool_slowly)
    opening = asyncio.ensure_future(client.create_connection(with_db=True))
    await pool_created.wait()
    await client.close()
    pool_released.set()
    await opening
    assert client._pool is None
    await wait_until(lambda: _has_no_sessions(application_name))


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_close_while_the_pool_sets_itself_up_opens_it_anew(db_isolated_no_schema):
    """A close between opening the pool and its first setup statement (the search_path check under
    transaction_pooling) left that statement waiting on the lock its own task held - the client hung."""
    application_name = f"hare_pool_setup_{uuid.uuid4().hex[:8]}"
    client = _make_pool_client(application_name, min_size=1, max_size=2)
    post_connect = client._post_connect
    closes: list[bool] = []

    async def post_connect_closed_first() -> None:
        if not closes:
            closes.append(True)
            await client.close()
            # A setup statement of the pool, run on it.
            await client.execute_dicts("SELECT 1 AS n")
        await post_connect()

    client._post_connect = post_connect_closed_first
    try:
        assert await asyncio.wait_for(client.execute_dicts("SELECT 1 AS n"), 10) == [{"n": 1}]
    finally:
        await client.close()
    await wait_until(lambda: _has_no_sessions(application_name))


@pytest.mark.requires("asyncpg")
@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_connections_still_opening_when_the_pool_closes_are_closed(db_isolated_no_schema):
    """A pool closed while it still opened connections for waiting queries left those connections
    connected once they finished opening."""
    import asyncpg

    db_config, _, is_rust_pg = _get_db_config()
    if is_rust_pg:
        pytest.skip("the asyncpg pool's own growth")
    application_name = f"hare_pool_growing_{uuid.uuid4().hex[:8]}"
    connect_calls = 0
    pool_closed = asyncio.Event()

    async def connect(*args: Any, **kwargs: Any) -> Any:
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls > 1:
            # Still opening when the pool closes.
            await pool_closed.wait()
        return await asyncpg.connect(*args, **kwargs)

    client = _make_pool_client(application_name, min_size=1, max_size=5, connect=connect)
    await client.create_connection(with_db=True)
    pool = client._pool
    first_connection = await pool.acquire()
    # The pool opens a connection for each of these - still opening when it closes.
    acquires = [asyncio.ensure_future(pool.acquire()) for _ in range(3)]
    await wait_until(lambda: connect_calls == 4)
    await pool.release(first_connection)
    await client.close()
    pool_closed.set()
    await asyncio.gather(*acquires, return_exceptions=True)
    await wait_until(lambda: _has_no_sessions(application_name))


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_statement_of_another_task_waits_for_the_pool_being_set_up(db_simple):
    """Another task's statement ran on a pool the opening task had published but still set up - before
    its server's features and the checks of the connection."""
    client = Connections.current().create_independent("models", {"min_size": 1, "max_size": 2})
    set_up = asyncio.Event()
    set_up_released = asyncio.Event()
    post_connect = client._post_connect

    async def slow_post_connect():
        await post_connect()
        await set_up_released.wait()
        set_up.set()

    client._post_connect = slow_post_connect
    try:
        opening = asyncio.create_task(client.execute_dicts("SELECT 1 AS n"))
        await wait_until(lambda: client._pool is not None)
        set_up_seen: list[bool] = []

        async def other_statement() -> list[dict[str, Any]]:
            rows = await client.execute_dicts("SELECT 2 AS n")
            set_up_seen.append(set_up.is_set())
            return rows

        other = asyncio.create_task(other_statement())
        # The other statement gets every chance to run before the set-up ends.
        for _ in range(100):
            await asyncio.sleep(0)
        set_up_released.set()
        assert await other == [{"n": 2}]
        assert set_up_seen == [True]
        assert await opening == [{"n": 1}]
    finally:
        await client.close()
