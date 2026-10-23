"""Skips of the tests meeting a fault of the SQLite library the suite runs on - hare leaves such faults
to the library."""

import sqlite3

import pytest

from tests.utils.database_under_test import DatabaseUnderTest

RUNS_ON_SQLITE = DatabaseUnderTest.get_engine_name(DatabaseUnderTest.get_dialect()) == "sqlite"

#: SQLite 3.38.0-3.41.0 build automatic indexes that ignore a comparison's collating sequence - a
#: comparison under hare's decimal or time collation over a joined table can find no rows.
SKIP_ON_AUTOMATIC_INDEX_COLLATION_FAULT = pytest.mark.skipif(
    RUNS_ON_SQLITE and (3, 38, 0) <= sqlite3.sqlite_version_info < (3, 41, 1),
    reason="SQLite 3.38.0-3.41.0 build automatic indexes that ignore a comparison's collating sequence",
)
#: SQLite 3.38.0-3.38.5 report a foreign key broken by a statement with RETURNING as a plain error.
SKIP_ON_RETURNING_FOREIGN_KEY_ERROR_FAULT = pytest.mark.skipif(
    RUNS_ON_SQLITE and (3, 38, 0) <= sqlite3.sqlite_version_info < (3, 39, 0),
    reason="SQLite 3.38.0-3.38.5 report a foreign key broken by a statement with RETURNING as a plain error",
)
