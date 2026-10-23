from __future__ import annotations

#: The oldest SQLite library hare runs on - ``RETURNING`` and ``ALTER TABLE ... DROP COLUMN`` first
#: appeared in 3.35.0; 3.35.5, the last 3.35, is the first without its known faults: ``ALTER TABLE ... RENAME
#: COLUMN`` rewriting the generated columns of other tables (fixed in 3.35.2), a correlated ``EXISTS``
#: filtering the wrong rows (fixed by 3.35.4), ``ALTER TABLE ... DROP COLUMN`` corrupting the database file
#: (fixed in 3.35.5).
SQLITE_MINIMUM_SERVER_VERSION = (3, 35, 5)
#: The first SQLite version with ``unhex()``.
SQLITE_UNHEX_SERVER_VERSION = (3, 41, 0)
#: The first SQLite version with ``STRICT`` tables.
SQLITE_STRICT_SERVER_VERSION = (3, 37, 0)
#: The first SQLite version with ``ORDER BY`` inside an aggregate's arguments.
SQLITE_ORDERED_AGGREGATES_SERVER_VERSION = (3, 44, 0)

#: The deepest expression the SQLite library parses (its SQLITE_MAX_EXPR_DEPTH).
SQLITE_MAX_EXPRESSION_DEPTH = 1000
