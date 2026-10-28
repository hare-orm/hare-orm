from __future__ import annotations

from pathlib import Path

from tests.matrix.suite_environment import SuiteEnvironment

#: The repository root.
REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[2]
#: Where each run's pytest output is written.
MATRIX_LOG_DIRECTORY = REPOSITORY_DIRECTORY / ".test-matrix"
#: The directory of the test suite - a ``conftest.py`` above it declares nothing for the tests.
TESTS_DIRECTORY = REPOSITORY_DIRECTORY / "tests"
#: The package of the test matrix - its environments and its runner, the tests of none.
MATRIX_PACKAGE = "tests.matrix"

#: pytest's option naming the environment a run is in (``SUITE_ENVIRONMENTS``).
SUITE_ENVIRONMENT_OPTION = "--hare-suite-environment"
#: How many runs of environments of extras go at once - each a few tests in one pytest process, which
#: collects the suite once, where pytest-xdist's workers each would; runs reaching a server go one at a
#: time.
FEW_TEST_RUNS_AT_ONCE = 4
#: The start of the name of each SQLite suite - ``sqlite-3.35.5``, ``sqlite-regexp``.
SQLITE_SUITE_PREFIX = "sqlite-"
#: The environment of a run naming none - nothing optional installed.
CORE_SUITE_ENVIRONMENT = "core"
#: The run of the tests of every environment at once - in a virtualenv installing everything (make deps,
#: the lowest dependencies' one).
EVERY_SUITE_ENVIRONMENT = "all"
#: The module variable declaring what the tests of a module or of a ``conftest.py``'s directory require.
REQUIRES_MODULE_NAME = "REQUIRES"
#: The marker of a test requiring extras or groups besides its module's.
REQUIRES_MARKER = "requires"
#: The requirement of a test needing hare's compiled extension - the pure wheel has none.
NATIVE_REQUIREMENT = "native"
#: The module of that extension.
NATIVE_MODULE = "rust.native"
#: The extra of each database's driver - it joins the virtualenv of a run on that database.
MATRIX_DATABASE_DRIVER_EXTRAS = {
    "sqlite": (),
    "postgresql": (),
    "postgresql+asyncpg": ("asyncpg",),
    "clickhouse+clickhouse-connect": ("clickhouse-connect",),
    "clickhouse+clickhouse-driver": ("clickhouse-driver",),
}
#: The database of each driver of the matrix's suites - ``MATRIX_POSTGRES_URLS``, ``MATRIX_CLICKHOUSE_URLS``.
MATRIX_DATABASES_BY_DRIVER = {
    "rust_pg": "postgresql",
    "asyncpg": "postgresql+asyncpg",
    "clickhouse-connect": "clickhouse+clickhouse-connect",
    "clickhouse-driver": "clickhouse+clickhouse-driver",
}
#: The pytest plugin hiding rust.native - the runs of an environment without it load it.
WITHOUT_NATIVE_PLUGIN = "tests.matrix.without_native"
#: Every database of the matrix.
ALL_MATRIX_DATABASES = tuple(MATRIX_DATABASE_DRIVER_EXTRAS)
#: The environments of extras - each installs only what its tests require; they run the tests reading no
#: database, their tests of a database run in the ``all`` environment.
EXTRA_SUITE_ENVIRONMENTS = (
    SuiteEnvironment("asyncpg", ("asyncpg",)),
    SuiteEnvironment("clickhouse-connect", ("clickhouse-connect",)),
    SuiteEnvironment("clickhouse-driver", ("clickhouse-driver",)),
    SuiteEnvironment("encryption", ("encryption",)),
    SuiteEnvironment("encryption-pydantic", ("encryption", "pydantic")),
    SuiteEnvironment("phone", ("phone",)),
    SuiteEnvironment("sqlite-vec", ("sqlite-vec",)),
    SuiteEnvironment("tzlocal", ("tzlocal",)),
    SuiteEnvironment("yaml", ("yaml",)),
    SuiteEnvironment("faker", ("faker",)),
    SuiteEnvironment("pytz", groups=("pytz",)),
    SuiteEnvironment("opentelemetry", ("opentelemetry",)),
    # Also the tests of hare.contrib.request_query: its extra installs pydantic alone.
    SuiteEnvironment("pydantic", ("pydantic",)),
    SuiteEnvironment("request-query-http", ("request-query", "http")),
    SuiteEnvironment("http", ("http",)),
    SuiteEnvironment("litestar", ("litestar",)),
    SuiteEnvironment("fastapi", ("fastapi",)),
    # FastAPI's test clients run on httpx.
    SuiteEnvironment("fastapi-http", ("fastapi", "http")),
    SuiteEnvironment("robyn", ("robyn",)),
    SuiteEnvironment("taskiq", ("taskiq",)),
    # The broker environments run on their brokers' servers - MATRIX_BROKERS.
    SuiteEnvironment("redis", ("redis",)),
    SuiteEnvironment("kafka", ("kafka",)),
    SuiteEnvironment("rabbitmq", ("rabbitmq",)),
    SuiteEnvironment("pytest-plugin", ("pytest",)),
    SuiteEnvironment("mypy", ("mypy",)),
    SuiteEnvironment("stubs", ("pyright",), ("pyright-cli",)),
    SuiteEnvironment("ipython", ("ipython",)),
)
#: The environments of the test suites. Every test of a database runs in ``all`` on every server version, as
#: a user installing extras runs hare; hare without its optional packages (``core``) and without
#: rust.native (``pure``) run on the newest server of each database - and with each extra alone on none.
SUITE_ENVIRONMENTS = (
    SuiteEnvironment(
        CORE_SUITE_ENVIRONMENT,
        databases=("sqlite", "postgresql", "clickhouse+clickhouse-connect", "clickhouse+clickhouse-driver"),
        whole_suite=True,
        on_newest_servers_only=True,
    ),
    # The pure wheel: no rust.native (pytest -p tests.matrix.without_native).
    SuiteEnvironment("pure", databases=("sqlite",), native=False, whole_suite=True, on_newest_servers_only=True),
    *EXTRA_SUITE_ENVIRONMENTS,
    SuiteEnvironment(
        EVERY_SUITE_ENVIRONMENT,
        tuple(dict.fromkeys(extra for environment in EXTRA_SUITE_ENVIRONMENTS for extra in environment.extras)),
        tuple(dict.fromkeys(group for environment in EXTRA_SUITE_ENVIRONMENTS for group in environment.groups)),
        databases=ALL_MATRIX_DATABASES,
        whole_suite=True,
        database_independent=False,
    ),
)

#: The Python versions of the matrix - each one's virtualenv is ``.venv-<version>``.
MATRIX_PYTHON_VERSIONS = ("3.12", "3.13", "3.14")
#: The PostgreSQL versions of the matrix - each one's server listens on ``54<version>``
#: (``make test_db_matrix_up``).
MATRIX_POSTGRES_VERSIONS = ("14", "15", "16", "17", "18")
#: The PostgreSQL connection URLs of the two drivers, ``{port}`` filled per server.
MATRIX_POSTGRES_URLS = {
    "asyncpg": "postgresql+asyncpg://postgres:postgres@127.0.0.1:{port}/test_{{}}",
    "rust_pg": "postgresql://postgres:postgres@127.0.0.1:{port}/test_{{}}",
}
#: The PgBouncer versions of the matrix - each minor version from the oldest hare supports, each one in
#: front of the newest PostgreSQL server of the matrix, the newest also in front of the oldest
#: (``make test_pgbouncer_matrix_up``).
MATRIX_PGBOUNCER_VERSIONS = ("1.22", "1.23", "1.24", "1.25", "1.26")
#: The query of a connection through PgBouncer: transaction pooling, and the server itself for what
#: needs a session of its own - ``{direct_port}`` filled per PostgreSQL server.
MATRIX_PGBOUNCER_URL_QUERY = "?transaction_pooling=true&direct_host=127.0.0.1&direct_port={direct_port}"
#: The ClickHouse versions of the matrix - each one with a server image from the oldest hare supports
#: (``make test_clickhouse_matrix_up``, ``make test_clickhouse_cluster_matrix_up``).
MATRIX_CLICKHOUSE_VERSIONS = ("25.8", "25.10", "25.12", "26.3", "26.7", "26.8", "26.9")
#: The ClickHouse connection URLs of the two drivers - clickhouse-connect over HTTP, clickhouse-driver
#: over the native protocol - ``{port}`` and ``{database}`` filled per server and suite.
MATRIX_CLICKHOUSE_URLS = {
    "clickhouse-connect": "clickhouse+clickhouse-connect://default:clickhouse@127.0.0.1:{port}/{database}",
    "clickhouse-driver": "clickhouse+clickhouse-driver://default:clickhouse@127.0.0.1:{port}/{database}",
}
#: The query of a connection to the test cluster: the cluster of two shards.
MATRIX_CLICKHOUSE_CLUSTER_URL_QUERY = "?cluster=hare_sharded"
#: The query of a ClickHouse server's URL: the server's own Keeper, the test databases' key series deleted with
#: them - a server keeps at most max_autoincrement_series series.
MATRIX_CLICKHOUSE_URL_QUERY = "?keeper_hosts={keeper_hosts}"
#: The tests of the ClickHouse suites - the dialect's own: the shared test models have keys the
#: database generates, which ClickHouse doesn't.
MATRIX_CLICKHOUSE_TEST_PATHS = (
    "tests/dialects/clickhouse",
    "tests/contracts",
    "tests/fields/value_types/test_ordered_values.py",
    "tests/fields/value_types/test_ip_address_text_lookups.py",
    "tests/fields/value_types/test_ip_address_zones.py",
)
#: The tests of the ClickHouse cluster suites.
MATRIX_CLICKHOUSE_CLUSTER_TEST_PATHS = ("tests/dialects/clickhouse/cluster",)
#: The broker versions of the matrix - the oldest hare supports and the newest of each
#: (``make test_brokers_matrix_up``): the outbox talks protocols the versions between keep.
MATRIX_REDIS_VERSIONS = ("7.2", "8.10")
MATRIX_RABBITMQ_VERSIONS = ("4.0", "4.3")
MATRIX_KAFKA_VERSIONS = ("3.9", "4.3")
#: Each broker of the matrix: its versions, the variable naming its server for the tests, the variable's
#: value (``{port}`` filled per server) and the first digits of its servers' ports.
MATRIX_BROKERS = {
    "redis": (MATRIX_REDIS_VERSIONS, "HARE_TEST_REDIS_URL", "redis://127.0.0.1:{port}/0", 16),
    "rabbitmq": (MATRIX_RABBITMQ_VERSIONS, "HARE_TEST_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:{port}/", 15),
    "kafka": (MATRIX_KAFKA_VERSIONS, "HARE_TEST_KAFKA_BOOTSTRAP", "127.0.0.1:{port}", 19),
}

#: pytest's options for every run.
MATRIX_PYTEST_OPTIONS = ("-q", "-p", "no:cacheprovider", "--tb=short", "--verify-plans")
#: The tests whose outcome depends on SQLite's optional regular expression functions - run once
#: more with them installed.
SQLITE_REGEXP_TEST_PATHS = (
    "tests/queries/filtering/test_posix_regex_filter.py",
    "tests/backends/test_sqlite_client.py",
)

#: The marker of the tests reading no test database: a suite of their own, left out of the suite
#: of every database.
DATABASE_INDEPENDENT_MARKER = "database_independent"
#: The name of that suite.
DATABASE_INDEPENDENT_SUITE = "database-independent"

#: pytest's summary counts (``12 passed``, ``3 skipped``, ...).
PYTEST_COUNT_PATTERN = r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)"
#: pytest's exit code of a run choosing no test - an environment with no tests in a suite passes it.
PYTEST_NO_TESTS_EXIT_CODE = 5
#: pytest's run time at the end of its summary line.
PYTEST_DURATION_PATTERN = r" in ([\d.]+)s"
