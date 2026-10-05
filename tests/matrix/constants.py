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

#: pytest's options for every run.
MATRIX_PYTEST_OPTIONS = ("-q", "-p", "no:cacheprovider", "--no-cov", "--tb=short", "--verify-plans")
#: The tests whose outcome depends on SQLite's optional regular expression functions - run once
#: more with them installed.
SQLITE_REGEXP_TEST_PATHS = ("tests/test_posix_regex_filter.py", "tests/backends/test_sqlite_client.py")

#: The marker of the tests reading no test database: a suite of their own, left out of the suite
#: of every database.
DATABASE_INDEPENDENT_MARKER = "database_independent"
#: The name of that suite.
DATABASE_INDEPENDENT_SUITE = "database-independent"

#: pytest's summary counts (``12 passed``, ``3 skipped``, ...).
PYTEST_COUNT_PATTERN = r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)"
#: pytest's run time at the end of its summary line.
PYTEST_DURATION_PATTERN = r" in ([\d.]+)s"
