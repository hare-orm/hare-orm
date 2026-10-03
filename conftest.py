"""
Pytest configuration for Hare ORM tests.

Uses function-scoped fixtures for true test isolation.
"""

import gc
import os
import re
import uuid
from typing import Any

import pytest
import pytest_asyncio

# Registers the columnar test dialect - HARE_TEST_DB=columnar://... and its own tests use it.
import tests.dialects.columnar  # noqa: F401
from hare.contrib.test import truncate_all_models
from hare.contrib.test.constants import REUSE_DATABASES_ENVIRONMENT_VARIABLE
from hare.contrib.test.helpers import hare_test_context
from hare.dialects.base.constants import SLOW_QUERY_THRESHOLD_MS
from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.instrumentation.observers import Observers


@pytest.fixture(autouse=True)
def _reset_slow_query_threshold():
    """Observers.slow_query_threshold_ms is process-global, not per-HareContext - a
    test that sets it (directly, or via Hare.init(slow_query_threshold_ms=...)) would otherwise
    leak that value into every later test, since hare_test_context()'s own ctx.init() never
    touches it (only Hare.init() does)."""
    yield
    Observers.slow_query_threshold_ms = SLOW_QUERY_THRESHOLD_MS


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def _resolve_db_url() -> str:
    """Reads HARE_TEST_DB and, if it contains a "{}" placeholder, fills it with a fresh
    per-call UUID.

    Unescapes "\\{"/"\\}" to "{"/"}" first, mirroring DbUrlConfigGenerator.expand()
    exactly - the Makefile's test_postgres_asyncpg
    target sets HARE_TEST_DB to ".../test_\\{\\}" (backslash-escaped), and without this same
    unescaping, "{}" in raw_db_url never matches, so the placeholder is never filled here. That
    was the real root cause of the "database does not exist" CI failures traced through this
    whole investigation: _shared_test_db_url handed out that STILL-templated string believing it
    was one stable, already-resolved database name; every downstream hare_test_context() call
    (once per test module, via db_module) runs the SAME raw URL through
    DbUrlConfigGenerator.expand() again, which unconditionally re-substitutes ITS OWN fresh
    uuid.uuid4().hex on every single call - so db_module was never actually reconnecting to the
    one database _shared_test_db_url had created, but to a brand new, never-created random name
    every time. Resolving (and de-templating) the URL exactly once, here, means every later
    expand() call sees no "{}" left to substitute and is a no-op, so the same resolved name is
    used consistently.
    """
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    raw_db_url = raw_db_url.replace("\\{", "{").replace("\\}", "}")
    return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url


def _is_file_database(db_url: str) -> bool:
    """Whether the DB_URL's driver keeps the database in a file (SQLite and the dialects built on
    it) - each test context then opens a fresh ":memory:" database of its own, where a database
    server's is created once per session and shared."""
    from hare.dialects.registry import DialectRegistry

    scheme = db_url.split("://", 1)[0]
    return DialectRegistry.get_driver_for_url_scheme(scheme).path_credential == "file_path"


async def _reset_shared_db_for_module(ctx) -> None:
    """Wipes every table holding rows (and, via CASCADE, every M2M through-table referencing one)
    and restarts every sequence that gave out a value - restoring the same "this file starts from
    a completely clean, freshly numbered database" guarantee db_module used to give by recreating
    the whole database per file. Only what a previous file left behind is touched: a TRUNCATE ...
    RESTART IDENTITY of the full ~130-model schema costs the server per table and sequence,
    used or not.

    Needed because db_module's own per-test isolation (the `db` fixture's transaction rollback)
    only protects against a SINGLE test's own writes - a test using Transactions.autonomous() (a
    genuinely separate, auto-committing connection, not rolled back by `db`) still leaves real,
    permanent rows behind once its own file is done with them. Under the old one-database-
    per-file scheme that was invisible (the next file got its own fresh database regardless);
    sharing one physical database across files makes it visible unless undone here.
    """
    if not ctx.apps:
        return
    tables = list(dict.fromkeys(model._meta.db_table for model in ctx.apps.get_models_iterable()))
    if not tables:
        return
    db = ctx.db()
    await db.dialect.clear_tables(db, [db.dialect.quote_identifier(table) for table in tables])
    used_sequences = await db.execute_dicts(
        "SELECT quote_ident(schemaname) || '.' || quote_ident(sequencename) AS sequence_name "
        "FROM pg_sequences WHERE last_value IS NOT NULL"
    )
    if used_sequences:
        await db.execute_script(
            ";\n".join(f"ALTER SEQUENCE {row['sequence_name']} RESTART" for row in used_sequences)  # nosec
        )


# ============================================================================
# PYTEST FIXTURES FOR TESTS
# These fixtures provide different isolation patterns for test scenarios
# ============================================================================


class OwnedTestDatabases:
    """Ties every Postgres test database a pytest process creates to that process's lifetime, so
    the start-of-session sweep of leftover test databases only ever drops the ones whose creating
    process is gone.

    Each process picks a random run tag, embeds it in every database name it creates (by rewriting
    HARE_TEST_DB's "{}" placeholder to "<run tag>_{}") and holds a session-level advisory lock keyed
    by that tag on an admin connection for its whole lifetime. Postgres releases the lock as soon as
    the process disconnects, however it ends (Ctrl-C, a killed agent, an OOM kill). A database whose
    tag's lock can be taken belongs to a finished run; one whose lock is held belongs to a live run -
    even while that run has no connection open to it (its shared database has none between two test
    modules), which is exactly when a "zero connections means orphaned" rule would drop it from under
    a concurrent run.
    """

    run_tag: str = uuid.uuid4().hex[:12]
    OWNED_NAME_PATTERN = re.compile(r"^test_([0-9a-f]{12})_[0-9a-f]{32}$")
    UNTAGGED_NAME_PATTERN = re.compile(r"^test_[0-9a-f]{32}$")
    #: An untagged name comes from a run that predates run tags; with no owner to ask, it is only
    #: dropped once it is far older than any real test run lasts.
    UNTAGGED_MINIMUM_AGE_SECONDS = 3 * 60 * 60
    UNTAGGED_URL_ENVIRONMENT_VARIABLE = "HARE_TEST_DB_UNTAGGED"
    APPLICATION_NAME_PREFIX = "hare_test_run_"
    #: The admin connection holding this process's ownership lock for the whole session.
    ownership_connection: Any = None
    #: Server credentials the admin connection was opened with.
    server_credentials: dict[str, Any] = {}

    @classmethod
    def get_lock_key(cls, run_tag: str) -> int:
        return int(run_tag, 16)

    @classmethod
    def tag_database_url_template(cls) -> None:
        """Rewrites HARE_TEST_DB's "{}" placeholder to "<run tag>_{}" for this process. The untagged
        original is kept in its own environment variable, so an xdist worker - which inherits the
        controller's already-tagged value - tags the original with its own run tag instead."""
        untagged_url = os.environ.get(cls.UNTAGGED_URL_ENVIRONMENT_VARIABLE) or os.environ.get("HARE_TEST_DB")
        if untagged_url is None:
            return
        os.environ[cls.UNTAGGED_URL_ENVIRONMENT_VARIABLE] = untagged_url
        template = untagged_url.replace("\\{", "{").replace("\\}", "}")
        if "{}" not in template or _is_file_database(template):
            return
        os.environ["HARE_TEST_DB"] = template.replace("{}", f"{cls.run_tag}_{{}}", 1)

    @classmethod
    async def connect(cls, database: str, **options: Any) -> Any:
        import asyncpg

        return await asyncpg.connect(
            host=cls.server_credentials["host"],
            port=cls.server_credentials["port"],
            user=cls.server_credentials["user"],
            password=cls.server_credentials["password"],
            database=database,
            **options,
        )

    @classmethod
    async def acquire_ownership(cls, raw_db_url: str) -> bool:
        """Opens the admin connection and takes this run's ownership lock on it.

        Returns:
            False when there is nothing to own (SQLite, no asyncpg, or no reachable server).
        """
        if _is_file_database(raw_db_url):
            return False
        try:
            import asyncpg  # noqa: F401
        except ImportError:
            return False
        cls.server_credentials = DbUrlConfigGenerator.expand(raw_db_url, testing=False)["credentials"]
        try:
            connection = await cls.connect(
                "postgres",
                server_settings={"application_name": f"{cls.APPLICATION_NAME_PREFIX}{cls.run_tag}"},
            )
        except Exception as exc:  # noqa: BLE001 - best-effort hygiene, never block the real test run
            print(f"[OwnedTestDatabases] could not connect: {exc}", flush=True)
            return False
        await connection.execute("SELECT pg_advisory_lock($1)", cls.get_lock_key(cls.run_tag))
        cls.ownership_connection = connection
        return True

    @classmethod
    async def release_ownership(cls) -> None:
        connection, cls.ownership_connection = cls.ownership_connection, None
        if connection is not None:
            await connection.close()

    @classmethod
    async def sweep_orphaned_databases(cls) -> None:
        """Drops every leftover test database whose creating run is gone - a run killed before its
        own DROP DATABASE ran, and every finished run's shared database (never dropped on exit, see
        _shared_test_db_url). Best-effort: a failure is only logged, never raised."""
        connection = cls.ownership_connection
        if connection is None:
            return
        try:
            candidate_rows = await connection.fetch(
                "SELECT datname FROM pg_database d WHERE datname LIKE 'test\\_%' "
                "AND NOT EXISTS (SELECT 1 FROM pg_stat_activity a WHERE a.datname = d.datname)"
            )
            dropped_count = 0
            for row in candidate_rows:
                if await cls.drop_database_if_orphaned(row["datname"]):
                    dropped_count += 1
            if dropped_count:
                print(f"[OwnedTestDatabases] dropped {dropped_count} orphaned test database(s)", flush=True)
        except Exception as exc:  # noqa: BLE001 - best-effort hygiene, never block the real test run
            print(f"[OwnedTestDatabases] sweep failed: {exc}", flush=True)

    @classmethod
    async def drop_database_if_orphaned(cls, database_name: str) -> bool:
        """Drops ``database_name`` if the run that created it is gone.

        Returns:
            Whether the database was dropped.
        """
        connection = cls.ownership_connection
        owned_name_match = cls.OWNED_NAME_PATTERN.match(database_name)
        if owned_name_match is not None:
            owner_run_tag = owned_name_match.group(1)
            if owner_run_tag == cls.run_tag:
                return False
            owner_lock_key = cls.get_lock_key(owner_run_tag)
            if not await connection.fetchval("SELECT pg_try_advisory_lock($1)", owner_lock_key):
                return False  # the owning run is still alive
            try:
                return await cls.drop_database(database_name)
            finally:
                await connection.execute("SELECT pg_advisory_unlock($1)", owner_lock_key)
        if cls.UNTAGGED_NAME_PATTERN.match(database_name) is None:
            return False
        try:
            is_old_enough = await connection.fetchval(
                "SELECT (pg_stat_file('base/' || oid || '/PG_VERSION')).modification "
                "< now() - make_interval(secs => $2) FROM pg_database WHERE datname = $1",
                database_name,
                float(cls.UNTAGGED_MINIMUM_AGE_SECONDS),
            )
        except Exception:  # noqa: BLE001 - no privilege to read the database's age: leave it alone
            return False
        if not is_old_enough:
            return False
        return await cls.drop_database(database_name)

    @classmethod
    async def drop_database(cls, database_name: str) -> bool:
        """Rolls back the database's leftover prepared transactions - they block DROP DATABASE and
        hold slots of the server-wide max_prepared_transactions pool every run shares - then drops
        the database.

        Returns:
            Whether the database was dropped.
        """
        connection = cls.ownership_connection
        try:
            prepared_rows = await connection.fetch(
                "SELECT gid FROM pg_prepared_xacts WHERE database = $1", database_name
            )
            if prepared_rows:
                database_connection = await cls.connect(database_name)
                try:
                    for prepared_row in prepared_rows:
                        escaped_gid = prepared_row["gid"].replace("'", "''")
                        await database_connection.execute(f"ROLLBACK PREPARED '{escaped_gid}'")
                finally:
                    await database_connection.close()
            await connection.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        except Exception:  # noqa: BLE001 - lost a race, or genuinely can't drop it right now
            return False
        return True


def pytest_configure(config: pytest.Config) -> None:
    OwnedTestDatabases.tag_database_url_template()
    # Postgres test databases are reset and reused instead of created and dropped per context
    # (every DROP DATABASE requests a checkpoint); set the variable to 0 to turn this off.
    os.environ.setdefault(REUSE_DATABASES_ENVIRONMENT_VARIABLE, "1")


def pytest_collection_finish(session: pytest.Session) -> None:
    # The collected test modules and their models live for the whole run - kept out of every
    # garbage collection, which would otherwise walk them again each time.
    gc.freeze()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _orphaned_test_database_sweep():
    """Runs once per session (once per xdist worker process), before any test database is
    created, regardless of which db fixture a test requests: takes this run's ownership of the
    databases it is about to create and drops the ones left behind by finished runs."""
    if not await OwnedTestDatabases.acquire_ownership(os.getenv("HARE_TEST_DB", "sqlite://:memory:")):
        yield
        return
    await OwnedTestDatabases.sweep_orphaned_databases()
    try:
        yield
    finally:
        await OwnedTestDatabases.release_ownership()


@pytest_asyncio.fixture(scope="session")
async def _shared_test_db_url(_orphaned_test_database_sweep: None):
    """
    Creates the test database and generates its schema ONCE for the whole session - only
    matters for Postgres (SQLite's in-memory case is already fast, and a fresh ":memory:" is a
    genuinely separate, empty database each time anyway, so there's nothing to share).

    Every db_module instance then connects to this SAME already-existing, already-schema'd
    database instead of each repeating CREATE DATABASE + ~171 CREATE TABLE/INDEX/constraint
    statements for the ~135 models in tests.testmodels - measured directly at ~0.56s per
    instance, which used to dominate the full Postgres suite's runtime (~85s of ~120s total,
    across ~150+ test files).

    Deliberately does NOT keep the creating HareContext open/"current" for the whole session the
    way naively making db_module itself session-scoped did in an earlier attempt at this: that
    left it as the ambient context for the entire run, and a test elsewhere that calls
    Hare.init() directly (e.g. tests/cli/test_cli.py's sqlmigrate coverage) found it "already
    current" and reinitialized it in place with unrelated (there, deliberately fake) connection
    details - corrupting the shared fixture for the rest of the session. Here, the database is
    created, the creating context exits immediately, and only the resulting connection URL (a
    plain string) is shared - each db_module instance still opens and closes its OWN
    short-lived HareContext, per module, exactly as before this fixture existed.

    Sharing one physical database across files also means a row committed outside the `db`
    fixture's own transaction rollback (most notably: a test using Transactions.autonomous(), a
    genuinely separate, auto-committing connection) would otherwise leak from one file into every
    later one - db_module's own postgres branch truncates every table (and resets every
    sequence back to 1) once per module to restore the "this file starts completely clean"
    guarantee a fresh-database-per-file used to give for free, at a fraction of the cost - see
    _reset_shared_db_for_module().

    Deliberately does NOT drop this database on exit (unlike a plain hare_test_context() call) -
    a `--durations` report on the full Postgres suite showed 30-70s attributed to "teardown" on
    whichever test happened to run last on a given xdist worker, which is this fixture's own
    session-end DROP DATABASE (a real network round trip) getting misattributed by pytest, not
    that test's own fault. Leaving the database in place here and letting the NEXT session's
    OwnedTestDatabases sweep drop it once this run is gone (already autouse, already runs every
    session regardless of which fixture a test requests - see _orphaned_test_database_sweep()) gets
    the exact same eventual cleanup without paying a synchronous DROP DATABASE on every single worker's critical path.
    """
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    if _is_file_database(raw_db_url):
        yield raw_db_url
        return

    resolved_db_url = _resolve_db_url()
    print(f"[_shared_test_db_url] creating {resolved_db_url}", flush=True)
    async with hare_test_context(
        modules=["tests.testmodels"],
        db_url=resolved_db_url,
        app_label="models",
        connection_label="models",
        _drop_db_on_exit=False,
    ):
        pass  # database + schema now exist; this context's own job is done, so it exits here
    print(f"[_shared_test_db_url] created + schema generated: {resolved_db_url}", flush=True)

    yield resolved_db_url


@pytest_asyncio.fixture(scope="module")
async def db_module(_shared_test_db_url):
    """
    Module-scoped fixture: Creates HareContext once per test module.

    This is the base fixture that creates the database schema once per module.
    Other fixtures build on top of this for different isolation strategies.

    On Postgres, connects to the database _shared_test_db_url already created and schema'd for
    the whole session instead of repeating that (expensive) work per module - see its own
    docstring. On SQLite, unchanged: a fresh ":memory:" database, created and schema'd here, same
    as before this fixture existed.

    Note: Uses connection_label="models" to match standard test infrastructure.
    """
    if _is_file_database(_shared_test_db_url):
        async with hare_test_context(
            modules=["tests.testmodels"],
            db_url=_shared_test_db_url,
            app_label="models",
            connection_label="models",
        ) as ctx:
            yield ctx
        return

    async with hare_test_context(
        modules=["tests.testmodels"],
        db_url=_shared_test_db_url,
        app_label="models",
        connection_label="models",
        _create_db=False,
        _generate_schemas=False,
        _drop_db_on_exit=False,
    ) as ctx:
        await _reset_shared_db_for_module(ctx)
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db(db_module):
    """
    Function-scoped fixture with transaction rollback cleanup.

    Each test runs inside a transaction that gets rolled back at the end,
    providing isolation without the overhead of schema recreation.

    For databases that don't support transactions (e.g., MySQL MyISAM),
    falls back to truncation cleanup.

    This is the FASTEST isolation method - use for most tests.

    Usage:
        @pytest.mark.asyncio
        async def test_something(db):
            obj = await Model.create(name="test")
            assert obj.id is not None
            # Changes are rolled back after test
    """
    # Get connection from the context using its default connection
    conn = db_module.db()

    # Check if the database supports transactions
    if conn.features.supports_transactions:
        # Start a savepoint/transaction
        transaction = conn._in_transaction()
        await transaction.__aenter__()

        try:
            yield db_module
        finally:
            # Rollback the transaction (discards all changes made during test)
            class _RollbackException(Exception):
                pass

            await transaction.__aexit__(_RollbackException, _RollbackException(), None)
    else:
        # For databases without transaction support (e.g., MyISAM),
        # fall back to truncation cleanup
        yield db_module
        await truncate_all_models(context=db_module)


@pytest_asyncio.fixture(scope="function")
async def db_simple(db_module):
    """
    Function-scoped fixture with NO cleanup between tests.

    Tests share state - data from one test persists to the next within the module.
    Use ONLY for read-only tests or tests that manage their own cleanup.

    Usage:
        @pytest.mark.asyncio
        async def test_read_only(db_simple):
            # Read-only operations, no writes
            config = get_config()
            assert "host" in config
    """
    yield db_module


@pytest_asyncio.fixture(scope="function")
async def db_isolated():
    """
    Function-scoped fixture with full database recreation per test.

    Creates a completely fresh database for EACH test. This is the SLOWEST
    method but provides maximum isolation.

    Use when:
    - Testing database creation/dropping
    - Tests need custom model modules
    - Tests must have completely clean state

    Usage:
        @pytest.mark.asyncio
        async def test_with_fresh_db(db_isolated):
            # Completely fresh database
            ...
    """
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(
        modules=["tests.testmodels"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_isolated_no_schema():
    """
    Same lifecycle as db_isolated (full per-test CREATE DATABASE + DROP) but without generating
    the ~135-model tests.testmodels schema - for tests that need their own genuinely fresh
    database/connection (e.g. to mutate connection-level config: SSL mode, application_name, GUC
    server settings) but never touch a testmodels table, so paying for that schema on every test
    is pure overhead (~0.56s each - see _shared_test_db_url's own docstring for how that number
    was measured).

    Usage:
        @pytest.mark.asyncio
        async def test_with_fresh_bare_db(db_isolated_no_schema):
            # Completely fresh database, no tests.testmodels schema
            ...
    """
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(
        modules=["tests.testmodels"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_truncate(db_module):
    """
    Function-scoped fixture with table truncation cleanup.

    After each test, all tables are truncated (TRUNCATE ... CASCADE on Postgres, a
    dependency-ordered DELETE elsewhere via truncate_all_models()).
    Faster than db_isolated but slower than db (transaction rollback).

    Use when testing transaction behavior (can't use rollback for cleanup).

    Usage:
        @pytest.mark.asyncio
        async def test_with_transactions(db_truncate):
            async with atomic():
                await Model.create(name="test")
            # Table truncated after test
    """
    yield db_module
    await truncate_all_models()


# ============================================================================
# HELPER FIXTURES
# ============================================================================


def make_db_fixture(modules: list[str], app_label: str = "models", connection_label: str = "models"):
    """
    Factory function to create custom db fixtures with different modules.

    Use this in subdirectory conftest.py files for tests that need
    custom model modules.

    Example usage in tests/fields/conftest.py:
        db_array = make_db_fixture(["tests.fields.test_array"])

    Args:
        modules: List of module paths to discover models from.
        app_label: The app label for the models, defaults to "models".
        connection_label: The connection alias name, defaults to "models".

    Returns:
        An async fixture function.
    """

    @pytest_asyncio.fixture(scope="function")
    async def _db_fixture():
        db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
        async with hare_test_context(
            modules=modules,
            db_url=db_url,
            app_label=app_label,
            connection_label=connection_label,
        ) as ctx:
            yield ctx

    return _db_fixture
