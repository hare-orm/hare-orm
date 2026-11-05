"""
Pytest configuration for Hare ORM tests.

Uses function-scoped fixtures for true test isolation.
"""

import gc
import os
import sqlite3
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_asyncio

from hare.contrib.test import RollbackIsolation, truncate_all_models
from hare.contrib.test.databases.postgresql.reusable_test_schemas import ReusableTestSchemas
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.base.constants import SLOW_QUERY_THRESHOLD_MS
from hare.instrumentation.observers.observers import Observers
from tests.matrix.constants import EVERY_SUITE_ENVIRONMENT, SUITE_ENVIRONMENT_OPTION
from tests.matrix.suite_environments import SuiteEnvironments
from tests.plan_verification.constants import PLAN_VERIFICATION_AFTER_TEST_MARKER, VERIFY_PLANS_OPTION
from tests.plan_verification.plan_coverage import PlanCoverage
from tests.plan_verification.plan_parts_verification import PlanPartsVerification
from tests.plan_verification.plan_verification import PlanVerification
from tests.resources.constants import UNTAGGED_URL_ENVIRONMENT_VARIABLE
from tests.resources.test_run_resources import TestRunResources
from tests.sqlite_versions.constants import SQLITE_VERSION_ENVIRONMENT_VARIABLE
from tests.utils.constants import (
    DECIMAL_FIELDS_MARKER,
    POSTGRESQL_MODELS_MARKER,
    RUN_TIMING_OPTION,
    SQLITE_ONLY_MARKER,
    TIMING_MARKER,
)
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.driver_classes import DriverClasses
from tests.utils.kept_shared_test_database import KeptSharedTestDatabase
from tests.utils.power_throttling import PowerThrottling

if TYPE_CHECKING:
    from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        VERIFY_PLANS_OPTION,
        action="store_true",
        help="Check every query run on a kept plan against the same query built in full.",
    )
    parser.addoption(
        RUN_TIMING_OPTION,
        action="store_true",
        help="Run the tests whose outcome depends on time too (the timing marker) - left out otherwise.",
    )
    parser.addoption(
        SUITE_ENVIRONMENT_OPTION,
        default=EVERY_SUITE_ENVIRONMENT,
        help=(
            "The environment of tests/matrix/constants.py SUITE_ENVIRONMENTS the run is in - only its tests run; "
            f"{EVERY_SUITE_ENVIRONMENT} (the default) runs every environment's tests, in a virtualenv installing "
            "everything."
        ),
    )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    # A module or directory of another environment isn't collected - its imports of packages this run
    # lacks never run. None leaves the decision to pytest's own rules.
    return True if SuiteEnvironments.is_other_environment_path(collection_path) else None


@pytest.fixture
def client_class(request: pytest.FixtureRequest) -> Any:
    """The driver class a test parametrized with ``DriverClasses.get_parameters()`` (``indirect=True``) runs on."""
    return DriverClasses.get_class(request.param)


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


def _get_test_database_url() -> str:
    """Read HARE_TEST_DB and fill its optional "{}" placeholder with a per-call UUID.

    Unescape "\\{"/"\\}" first, as TestDatabaseConfig.from_url() does, accepting the Makefile's
    shell-escaped URLs. The shared database fixture distributes this concrete URL to its
    modules, which reconnect to the same database.
    """
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    raw_db_url = raw_db_url.replace("\\{", "{").replace("\\}", "}")
    return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url


# ============================================================================
# PYTEST FIXTURES FOR TESTS
# These fixtures provide different isolation patterns for test scenarios
# ============================================================================


def pytest_configure(config: pytest.Config) -> None:
    # Runs in every xdist worker too - a worker Windows throttles runs at half the speed.
    PowerThrottling.turn_off()
    # The shared models of the run's environment (KeptSharedTestDatabase.get_modules()) read it.
    SuiteEnvironments.set_current(config)
    # A run on a prepared SQLite version (tests/sqlite_versions) stops when another one is loaded -
    # a missing library would otherwise silently test the system's own SQLite.
    required_sqlite_version = os.environ.get(SQLITE_VERSION_ENVIRONMENT_VARIABLE)
    if required_sqlite_version and sqlite3.sqlite_version != required_sqlite_version:
        pytest.exit(
            f"{SQLITE_VERSION_ENVIRONMENT_VARIABLE}={required_sqlite_version}, but SQLite "
            f"{sqlite3.sqlite_version} is loaded - prepare it with tests/sqlite_versions/download_sqlite_versions.py",
            returncode=4,
        )
    if config.getoption(VERIFY_PLANS_OPTION):
        PlanVerification.install()
        PlanCoverage.install()
        PlanPartsVerification.install()
    resources = TestRunResources()
    config.stash[TestRunResources.key] = resources
    resources.configure()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Any:
    if not PlanVerification.originals:
        return (yield)
    # A test counting builds compares its runs on a plan after its body, before its fixtures are
    # torn down.
    with PlanVerification.deferring(item.get_closest_marker(PLAN_VERIFICATION_AFTER_TEST_MARKER) is not None):
        return (yield)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # A mismatch raised inside a test that expected some exception still fails the run.
    if PlanVerification.mismatches and session.exitstatus == 0:
        session.exitstatus = 1
    # A pytest-xdist worker hands its plan coverage counts to the controller.
    worker_output = getattr(session.config, "workeroutput", None)
    if worker_output is not None:
        worker_output["plan_coverage"] = (PlanCoverage.made, PlanCoverage.built_without_plan)


def pytest_testnodedown(node: Any, error: Any) -> None:
    made, built_without_plan = getattr(node, "workeroutput", {}).get("plan_coverage", (0, 0))
    PlanCoverage.made += made
    PlanCoverage.built_without_plan += built_without_plan


def pytest_terminal_summary(terminalreporter: Any) -> None:
    for mismatch in dict.fromkeys(PlanVerification.mismatches):
        terminalreporter.write_line(f"PLAN MISMATCH: {mismatch}")
    if PlanCoverage.original is not None:
        terminalreporter.write_line(PlanCoverage.get_summary(PlanCoverage.made, PlanCoverage.built_without_plan))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    left_out_markers = []
    # A test opening only SQLite databases of its own runs the same on every database under test.
    if DatabaseUnderTest.get_engine_name(DatabaseUnderTest.get_dialect()) != "sqlite":
        left_out_markers.append(SQLITE_ONLY_MARKER)
    # The models only PostgreSQL's shared test database holds.
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        left_out_markers.append(POSTGRESQL_MODELS_MARKER)
    # Exact decimals, on a database storing none.
    if not DatabaseUnderTest.holds_decimal_fields():
        left_out_markers.append(DECIMAL_FIELDS_MARKER)
    # A test whose outcome depends on time runs only when asked for.
    if not config.getoption(RUN_TIMING_OPTION):
        left_out_markers.append(TIMING_MARKER)
    deselected = [
        item for item in items if any(item.get_closest_marker(marker) is not None for marker in left_out_markers)
    ]
    # The tests of another environment - they require what this run's doesn't install, or don't require what
    # it does.
    deselected_ids = {id(item) for item in deselected}
    deselected.extend(
        item for item in SuiteEnvironments.get_other_environment_items(items) if id(item) not in deselected_ids
    )
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        deselected_ids = {id(item) for item in deselected}
        items[:] = [item for item in items if id(item) not in deselected_ids]


def pytest_collection_finish(session: pytest.Session) -> None:
    # The collected test modules and their models live for the whole run - kept out of every
    # garbage collection, which would otherwise walk them again each time.
    gc.freeze()


def pytest_unconfigure(config: pytest.Config) -> None:
    resources = config.stash.get(TestRunResources.key, None)
    if resources is not None:
        resources.restore_environment()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def test_run_resources(request: pytest.FixtureRequest):
    """Keep session resources alive until every dependent database fixture has finished."""
    resources = request.config.stash[TestRunResources.key]
    await resources.start()
    try:
        yield resources
    finally:
        await resources.close()


@pytest_asyncio.fixture(scope="session")
async def _shared_test_db_url(test_run_resources: TestRunResources):
    """Share a concrete database URL without retaining an ambient HareContext.

    File databases pass through to each module fixture. A server database eligible for a kept lease
    is reused when its schema matches; otherwise this fixture creates one database for the session.
    Module contexts reset rows and close their own clients. Kept leases and disposable databases
    follow their resource owners' cleanup policies.
    """
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if DatabaseUnderTest.is_file_database(raw_db_url):
        yield raw_db_url
        return

    untagged_db_url = os.environ.get(UNTAGGED_URL_ENVIRONMENT_VARIABLE, raw_db_url)
    untagged_db_url = untagged_db_url.replace("\\{", "{").replace("\\}", "}")
    # Past a transaction pooler none is kept: a kept database dropped and created again by a run going
    # straight to the server leaves the pooler its server connections to the dropped one.
    keeps_database = (
        test_run_resources.connection is not None
        and "{}" in untagged_db_url
        and not DatabaseUnderTest.pools_server_sessions(untagged_db_url)
    )
    if keeps_database:
        resolved_db_url = await KeptSharedTestDatabase.acquire_url(test_run_resources.connection, untagged_db_url)
        if await KeptSharedTestDatabase.take(resolved_db_url):
            print(f"[_shared_test_db_url] kept from an earlier run: {resolved_db_url}", flush=True)
            yield resolved_db_url
            return
        await test_run_resources.drop_database(
            DbUrlConfigGenerator.expand(resolved_db_url)["credentials"]["database"], force=True
        )
    else:
        resolved_db_url = _get_test_database_url()
    print(f"[_shared_test_db_url] creating {resolved_db_url}", flush=True)
    async with hare_test_context(
        modules=KeptSharedTestDatabase.get_modules(),
        db_url=resolved_db_url,
        app_label="models",
        connection_label="models",
        _drop_db_on_exit=False,
    ) as context:
        if keeps_database:
            await KeptSharedTestDatabase.keep(context)
    print(f"[_shared_test_db_url] created + schema generated: {resolved_db_url}", flush=True)

    yield resolved_db_url


@pytest_asyncio.fixture(scope="module")
async def db_module(_shared_test_db_url):
    """
    Module-scoped context that the function-scoped isolation fixtures share.

    Server databases reuse the schema prepared by _shared_test_db_url, resetting rows on entry.
    File databases use hare_test_context's creation and cleanup lifecycle; an in-memory SQLite
    URL creates a fresh database and schema per module.

    Note: Uses connection_label="models" to match standard test infrastructure.
    """
    if DatabaseUnderTest.is_file_database(_shared_test_db_url):
        async with hare_test_context(
            modules=KeptSharedTestDatabase.get_modules(),
            db_url=_shared_test_db_url,
            app_label="models",
            connection_label="models",
        ) as ctx:
            yield ctx
        return

    async with hare_test_context(
        modules=KeptSharedTestDatabase.get_modules(),
        db_url=_shared_test_db_url,
        app_label="models",
        connection_label="models",
        _create_db=False,
        _generate_schemas=False,
        _drop_db_on_exit=False,
    ) as ctx:
        await ReusableTestSchemas.reset_rows(cast("PostgresqlClient", ctx.get_connection()))
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db(db_module):
    """
    Function-scoped fixture with transaction rollback cleanup.

    Each test runs inside a transaction that gets rolled back at the end,
    providing isolation without the overhead of schema recreation.

    For databases that don't support transactions,
    falls back to truncation cleanup.

    Use for tests that do not need a real commit or an independent transaction.

    Usage:
        @pytest.mark.asyncio
        async def test_something(db):
            obj = await Book.objects.create(name="test")
            assert obj.id is not None
            # Changes are rolled back after test
    """
    async with RollbackIsolation(db_module) as context:
        yield context


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
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=KeptSharedTestDatabase.get_modules(),
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_isolated_no_schema():
    """An isolated database and context without generating the shared models' schema.

    Uses the same creation or reusable-lease lifecycle as db_isolated. Lifecycle and
    connection-only tests can change connection settings without creating unused tables.

    Usage:
        @pytest.mark.asyncio
        async def test_with_fresh_bare_db(db_isolated_no_schema):
            # Isolated database, no shared-model tables
            ...
    """
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=KeptSharedTestDatabase.get_modules(),
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
            async with Transactions.atomic():
                await Book.objects.create(name="test")
            # Table truncated after test
    """
    yield db_module
    await truncate_all_models(context=db_module, connections=db_module.connections.all())
