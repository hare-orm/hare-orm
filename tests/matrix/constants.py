from __future__ import annotations

from tests.matrix.matrix_catalog import MatrixCatalog

#: pytest's option naming the environment a run is in (``SUITE_ENVIRONMENTS``).
SUITE_ENVIRONMENT_OPTION = "--hare-suite-environment"
#: How many runs of environments of extras go at once - each a few tests in one pytest process, which
#: collects the suite once, where pytest-xdist's workers each would. Runs sharing a physical server
#: are serialized.
FEW_TEST_RUNS_AT_ONCE = 4
#: The start of the name of each SQLite suite - ``sqlite-3.35.5``, ``sqlite-regexp``.
SQLITE_SUITE_PREFIX = "sqlite-"
#: The environment of a run naming none - nothing optional installed.
CORE_SUITE_ENVIRONMENT = "core"
#: The run of the tests of every environment at once - in a virtualenv installing everything (make deps,
#: the lowest dependencies' one).
EVERY_SUITE_ENVIRONMENT = "all"
#: The marker of a test requiring extras or groups besides its module's.
REQUIRES_MARKER = "requires"
#: The requirement of a test needing hare's compiled extension - the pure wheel has none.
NATIVE_REQUIREMENT = "native"
#: The module of that extension.
NATIVE_MODULE = "rust.native"
#: The extra of each database's driver - it joins the virtualenv of a run on that database.
MATRIX_DATABASE_DRIVER_EXTRAS = {
    name: tuple(extras) for name, extras in MatrixCatalog.get("database_driver_extras").items()
}
#: The database of each driver of the matrix's suites - ``MATRIX_POSTGRES_URLS``, ``MATRIX_CLICKHOUSE_URLS``.
MATRIX_DATABASES_BY_DRIVER = dict(MatrixCatalog.get("databases_by_driver"))
#: The pytest plugin hiding rust.native - the runs of an environment without it load it.
WITHOUT_NATIVE_PLUGIN = "tests.matrix.without_native"
#: The environments of the test suites. Every test of a database runs in ``all`` on every server version, as
#: a user installing extras runs hare; hare without its optional packages (``core``) and without
#: rust.native (``pure``) run on the newest server of each database - and with each extra alone on none.
SUITE_ENVIRONMENTS = MatrixCatalog.get_environments()

#: The Python versions of the matrix - each one's virtualenv is ``.venv-<version>``.
MATRIX_PYTHON_VERSIONS = tuple(MatrixCatalog.get("versions.python"))
#: The PostgreSQL versions of the matrix - each one's server listens on ``54<version>``
#: (``make test_db_matrix_up``).
MATRIX_POSTGRES_VERSIONS = tuple(MatrixCatalog.get("versions.postgres"))
#: The PostgreSQL connection URLs of the two drivers, ``{port}`` filled per server.
MATRIX_POSTGRES_URLS = dict(MatrixCatalog.get("urls.postgres"))
#: The PgBouncer versions of the matrix - each minor version from the oldest hare supports, each one in
#: front of the newest PostgreSQL server of the matrix, the newest also in front of the oldest
#: (``make test_pgbouncer_matrix_up``).
MATRIX_PGBOUNCER_VERSIONS = tuple(MatrixCatalog.get("versions.pgbouncer"))
#: The query of a connection through PgBouncer: transaction pooling, and the server itself for what
#: needs a session of its own - ``{direct_port}`` filled per PostgreSQL server.
MATRIX_PGBOUNCER_URL_QUERY = MatrixCatalog.get("url_queries.pgbouncer")
#: The ClickHouse versions of the matrix - each one with a server image from the oldest hare supports
#: (``make test_clickhouse_matrix_up``, ``make test_clickhouse_cluster_matrix_up``).
MATRIX_CLICKHOUSE_VERSIONS = tuple(MatrixCatalog.get("versions.clickhouse"))
#: The ClickHouse connection URLs of the two drivers - clickhouse-connect over HTTP, clickhouse-driver
#: over the native protocol - ``{port}`` and ``{database}`` filled per server and suite.
MATRIX_CLICKHOUSE_URLS = dict(MatrixCatalog.get("urls.clickhouse"))
#: The query of a connection to the test cluster: the cluster of two shards.
MATRIX_CLICKHOUSE_CLUSTER_URL_QUERY = MatrixCatalog.get("url_queries.clickhouse_cluster")
#: The query of a ClickHouse server's URL: the server's own Keeper, the test databases' key series deleted with
#: them - a server keeps at most max_autoincrement_series series.
MATRIX_CLICKHOUSE_URL_QUERY = MatrixCatalog.get("url_queries.clickhouse")
#: ClickHouse's dialect, contracts and selected shared suites; other shared models require unsupported
#: uniqueness and foreign-key constraints. Generated keys use Keeper.
MATRIX_CLICKHOUSE_TEST_PATHS = tuple(MatrixCatalog.get("paths.clickhouse"))
#: The tests of the ClickHouse cluster suites.
MATRIX_CLICKHOUSE_CLUSTER_TEST_PATHS = tuple(MatrixCatalog.get("paths.clickhouse_cluster"))
#: Each broker of the matrix: its versions, the variable naming its server for the tests, the variable's
#: value (``{port}`` filled per server) and the first digits of its servers' ports.
MATRIX_BROKERS = {
    name: (tuple(MatrixCatalog.get(f"versions.{name}")), broker["variable"], broker["url"], broker["port_prefix"])
    for name, broker in MatrixCatalog.get("brokers").items()
}

#: pytest's options for every run.
MATRIX_PYTEST_OPTIONS = tuple(MatrixCatalog.get("pytest.options"))
#: The tests whose outcome depends on SQLite's optional regular expression functions - run once
#: more with them installed.
SQLITE_REGEXP_TEST_PATHS = tuple(MatrixCatalog.get("paths.sqlite_regexp"))

#: The marker of the tests reading no test database: a suite of their own, left out of the suite
#: of every database.
DATABASE_INDEPENDENT_MARKER = "database_independent"
#: The name of that suite.
DATABASE_INDEPENDENT_SUITE = "database-independent"

RUN_REPORT_ENVIRONMENT_VARIABLE = "HARE_MATRIX_RUN_REPORT"
RUN_REPORT_PACKAGES = ("pytest", "pytest-xdist", "pytest-randomly", "hare-orm")
PROCESS_STOP_TIMEOUT_SECONDS = 5
