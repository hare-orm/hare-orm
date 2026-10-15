from __future__ import annotations

#: The oldest SQLite library hare runs on - ``RETURNING`` and ``ALTER TABLE ... DROP COLUMN`` first
#: appeared in 3.35.0; 3.35.5, the last 3.35, is the first without its known faults: ``ALTER TABLE ... RENAME
#: COLUMN`` rewriting the generated columns of other tables (fixed in 3.35.2), a correlated ``EXISTS``
#: filtering the wrong rows (fixed by 3.35.4), ``ALTER TABLE ... DROP COLUMN`` corrupting the database file
#: (fixed in 3.35.5).
SQLITE_MINIMUM_SERVER_VERSION = (3, 35, 5)
#: The first SQLite version whose ``ALTER TABLE ... DROP COLUMN`` is trusted - 3.35.5 fixed the ways
#: the earlier ones could corrupt the database file.
SQLITE_DROP_COLUMN_SERVER_VERSION = (3, 35, 5)
#: The first SQLite version with ``unhex()``.
SQLITE_UNHEX_SERVER_VERSION = (3, 41, 0)
#: The first SQLite version with ``STRICT`` tables.
SQLITE_STRICT_SERVER_VERSION = (3, 37, 0)
#: The first SQLite version with ``ORDER BY`` inside an aggregate's arguments.
SQLITE_ORDERED_AGGREGATES_SERVER_VERSION = (3, 44, 0)
#: The SQLite versions whose automatic index ignores the collating sequence of the comparison it
#: serves - a comparison under hare's decimal or time collation over a joined table finds no rows:
#: the first faulty version and the first fixed one.
SQLITE_AUTOMATIC_INDEX_COLLATION_FAULT_SERVER_VERSIONS = ((3, 38, 0), (3, 41, 1))
#: The SQLite versions reporting a foreign key broken by a statement with ``RETURNING`` as a plain
#: error (``SQLITE_ERROR``) instead of a constraint failure: the first faulty version and the first
#: fixed one.
SQLITE_RETURNING_FOREIGN_KEY_ERROR_FAULT_SERVER_VERSIONS = ((3, 38, 0), (3, 39, 0))
