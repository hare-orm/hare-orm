from __future__ import annotations

from pathlib import Path

#: The repository root.
REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[2]
#: Where each run's pytest output is written.
MATRIX_LOG_DIRECTORY = REPOSITORY_DIRECTORY / ".test-matrix"

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
#: The tests of the ClickHouse suites - the dialect's own: the shared test models have keys the
#: database generates, which ClickHouse doesn't.
MATRIX_CLICKHOUSE_TEST_PATHS = (
    "tests/dialects/clickhouse",
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
#: The tests of the broker suites - each suite sets one broker's variable, the others' tests skip.
MATRIX_BROKER_TEST_PATHS = ("tests/contrib/outbox/test_brokers.py",)

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
#: pytest's run time at the end of its summary line.
PYTEST_DURATION_PATTERN = r" in ([\d.]+)s"
