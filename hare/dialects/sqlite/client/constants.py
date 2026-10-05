from __future__ import annotations

from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.enums import ConnectionOptionType
from hare.dialects.sqlite.enums import SpatialiteMetadata

#: sqlite3.OperationalError's message for a statement binding more parameters than
#: SQLITE_LIMIT_VARIABLE_NUMBER allows.
SQLITE_TOO_MANY_VARIABLES_MESSAGE = "too many SQL variables"

#: sqlite3.OperationalError message fragments for a statement joining more tables than SQLite
#: allows (64 tables in one join, 200 FROM-clause terms).
SQLITE_TOO_MANY_JOINED_TABLES_MESSAGES = ("tables in a join", "too many FROM clause terms")

#: sqlite3's messages of an error a function raised - ``ValueError`` and the others, and
#: ``OverflowError`` - whatever the function's own message.
SQLITE_FUNCTION_ERROR_MESSAGES = frozenset({"user-defined function raised exception", "string or blob too big"})

#: SQLite's message for a LIKE or GLOB pattern longer than SQLITE_MAX_LIKE_PATTERN_LENGTH (50000 bytes by
#: default).
SQLITE_LIKE_PATTERN_TOO_LONG_MESSAGE = "LIKE or GLOB pattern too complex"
#: The error of such a pattern - a value too long to search for.
SQLITE_LIKE_PATTERN_TOO_LONG_ERROR = (
    "The text searched for is longer than a LIKE pattern of SQLite holds (50000 bytes by default) - {error}"
)

#: SQLite's message for an ON CONFLICT target no primary key or unique index of the table covers.
SQLITE_MISSING_CONFLICT_TARGET_MESSAGE = "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint"

#: SQLite's LIKE ignores ASCII case by default - switched on, or __contains/__startswith/__endswith
#: would match as __icontains does. The case-insensitive lookups wrap both sides in UPPER
#: function and don't depend on it.
SQLITE_DEFAULT_CASE_SENSITIVE_LIKE = "ON"


#: The exact, stable message SQLite's own C source uses when a DELETE's native ON DELETE CASCADE
#: recurses past SQLITE_LIMIT_TRIGGER_DEPTH - see SqliteTriggerRecursionLimitError.
SQLITE_TRIGGER_RECURSION_LIMIT_MESSAGE = "too many levels of trigger recursion"

#: How long close() waits for the transaction or query holding the connection to finish before
#: closing it anyway.
SQLITE_CLOSE_TIMEOUT_SECONDS = 10

#: SQLite's primary result code SQLITE_READONLY - a write the connection refuses (`PRAGMA query_only`).
SQLITE_READONLY_RESULT_CODE = 8

# The kwargs client construction reads itself - every other kwarg is a PRAGMA name.
# connect_max_retries/connect_retry_backoff_base_seconds are accepted and unused, so a DB_URL shared
# with another backend still works.
NON_PRAGMA_KWARGS = frozenset(
    {
        "connection_alias",
        "install_regexp_functions",
        "load_sqlite_vec",
        "load_spatialite",
        "spatialite_path",
        "spatialite_proj_database",
        "spatialite_metadata",
        "connect_max_retries",
        "connect_retry_backoff_base_seconds",
        "attached_databases",
    }
)

#: The connection setting naming the databases each connection attaches - ``{name: file}``, or in a
#: database URL ``name:file`` pairs separated by commas. A model's ``Meta.schema`` names one of them.
SQLITE_ATTACHED_DATABASES_OPTION_NAME = "attached_databases"

#: The names SQLite keeps for its own databases - no attached one takes them.
SQLITE_RESERVED_DATABASE_NAMES = frozenset({"main", "temp"})

#: Attaches a database file to a connection under a name.
SQLITE_ATTACH_DATABASE_SQL = "ATTACH DATABASE {file} AS {name};"

SQLITE_SPATIAL_LIBRARY_OPTION = ConnectionOption("spatialite_path", ConnectionOptionType.TEXT)

SQLITE_SPATIAL_PROJ_DATABASE_OPTION = ConnectionOption("spatialite_proj_database", ConnectionOptionType.TEXT)

#: The SpatiaLite library loaded when ``spatialite_path`` isn't given.
SQLITE_SPATIAL_DEFAULT_LIBRARY = "mod_spatialite"

#: The connection setting choosing the spatial metadata hare creates in a database without any - its
#: reference systems give the ellipsoid a geography is measured on, and register a spatial index's
#: column.
SQLITE_SPATIAL_METADATA_OPTION = ConnectionOption(
    "spatialite_metadata",
    ConnectionOptionType.CHOICE,
    choices=tuple(metadata.value for metadata in SpatialiteMetadata),
)

#: The interactive client ``hare dbshell`` opens.
SQLITE_SHELL_PROGRAM = "sqlite3"

#: How a file name names an in-memory database - no other program can open one.
SQLITE_IN_MEMORY_URI_MARKERS = (":memory:", "mode=memory")

#: The file beside a database whose OS lock ``migrate`` holds for its whole run - SQLite's own sidecar
#: files (``-journal``, ``-wal``) are named so too.
SQLITE_MIGRATION_LOCK_SUFFIX = "-migrate-lock"
#: Seconds between two tries of a ``migrate`` waiting for another one's migration lock.
SQLITE_MIGRATION_LOCK_POLL_SECONDS = 0.05
#: ``os.name`` on Windows - which locks a file by ``msvcrt``, not ``fcntl``.
WINDOWS_OS_NAME = "nt"
