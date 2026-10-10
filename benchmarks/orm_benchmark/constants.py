from __future__ import annotations

from pathlib import Path

BENCHMARK_DIRECTORY = Path(__file__).resolve().parent.parent
REPOSITORY = BENCHMARK_DIRECTORY.parent
CHART_DIRECTORY = REPOSITORY / "docs" / "assets" / "benchmarks"
RESULTS_FILE = CHART_DIRECTORY / "results.json"
#: The docs pages the benchmark writes: the overview and one page per database, by language.
DOCS_DIRECTORY = REPOSITORY / "docs" / "benchmarks"

#: The values of the widgets' ``category`` column, one per tenth of the table.
CATEGORIES = [f"cat{index}" for index in range(10)]
#: Every ORM gets the same pool - their defaults differ (5 to 16), and a smaller pool would starve
#: the load tests' 50 workers instead of measuring the ORM.
POOL_MAX_SIZE = 50
#: The widget table's rows by ``--size``.
SIZES = {"small": 100, "large": 1000}
#: The ClickHouse event table's rows by ``--size``, and how many of them the timed insert writes.
CLICKHOUSE_ROWS = {"small": 20_000, "large": 1_000_000}
CLICKHOUSE_INSERT_ROWS = {"small": 2_000, "large": 100_000}
#: The rows of the large bulk_create, as a multiple of the table's rows.
LARGE_BULK_CREATE_FACTOR = 10
LOAD_CONCURRENCY = 50
LOAD_OPERATIONS = 1000
#: The indices after which the operations of every load test repeat their types - ``index % 20`` of
#: the read load, ``% 2`` of the write load, ``% 5`` of the ClickHouse load.
LOAD_OPERATION_CYCLE = 20
#: How many times the warm-up runs every operation of the cycle on every worker at once.
LOAD_WARM_UP_ROUNDS = 2

#: The summary compares every target with this package's fastest driver of the database (see
#: ``ChartWriter.get_baseline()``); its other drivers are compared with it too.
BASELINE_DISTRIBUTION = "hare-orm"

#: The servers a run measures against, at 127.0.0.1 - `make test_db_up` and `make test_clickhouse_up`.
POSTGRESQL_PORT = 5433
CLICKHOUSE_HTTP_PORT = 8124
CLICKHOUSE_NATIVE_PORT = 9124
CLICKHOUSE_PASSWORD = "clickhouse"
#: The interpreter of a target's own virtual environment (``Target.environment``), under the
#: repository: ``.bench-venv-<environment>``.
ENVIRONMENT_DIRECTORY_PREFIX = ".bench-venv-"

#: The ClickHouse event table's values: a site per event out of 20, a user out of 10 000, an event type
#: out of five, a moment in the 30 days from CLICKHOUSE_FIRST_DAY.
CLICKHOUSE_SITE_COUNT = 20
CLICKHOUSE_USER_COUNT = 10_000
CLICKHOUSE_EVENT_TYPES = ("view", "click", "buy", "share", "like")
CLICKHOUSE_FIRST_DAY = "2026-01-01"
CLICKHOUSE_DAY_COUNT = 30
#: The site of the rows the timed insert writes - cleared before each repetition.
CLICKHOUSE_INSERTED_SITE = "inserted"

#: The rows of the summary table, in order - ``Target.family``.
FAMILIES = (
    "hare-orm",
    "SQLAlchemy",
    "SQLAlchemy (autocommit)",
    "tortoise-orm",
    "yara-orm",
    "Django",
    "clickhouse-connect",
)
#: The parts of a target's chart name the Russian charts and pages translate.
RUSSIAN_NAME_PARTS = {"(Rust driver)": "(драйвер на Rust)", "(no ORM)": "(без ORM)"}
#: The prefixes of the load tests' figures in the results - the read-heavy load and the write load.
LOAD_TESTS = ("load_test", "write_load_test")
