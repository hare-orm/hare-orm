from __future__ import annotations

#: Rows changed by the most recent INSERT/UPDATE/DELETE statement itself - trigger and
#: foreign-key action changes excluded.
SQLITE_LAST_STATEMENT_CHANGES_SQL = "SELECT changes()"

#: The first placeholder of a statement a dialect with numbered placeholders built - the SQLite
#: dialect's own statements (introspection, its service SQL) use ``?`` under any dialect.
SQLITE_FIRST_NUMBERED_PLACEHOLDER = "?1"

#: The text of a DBConnectionError of a database file SQLite can't open.
SQLITE_OPEN_FAILED_MESSAGE = "Can't open the SQLite database {filename}. Exception: {error}"

#: How many rows stream() reads off a SQLite cursor per hop to the worker thread when no
#: chunk_size is given.
SQLITE_STREAM_BATCH_SIZE = 1000

#: Whether the library was built with the FTS5 full-text search module.
SQLITE_FTS5_COMPILE_OPTION_SQL = "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
