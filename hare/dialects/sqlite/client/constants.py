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

#: SQLite's message for a statement breaking a foreign key.
SQLITE_FOREIGN_KEY_FAILED_MESSAGE = "FOREIGN KEY constraint failed"

#: SQLite's LIKE ignores ASCII case by default - switched on, so __contains/__startswith/__endswith
#: match as on Postgres. The case-insensitive lookups wrap both sides in the hare_upper function and
#: don't depend on it.
SQLITE_DEFAULT_CASE_SENSITIVE_LIKE = "ON"

SQLITE_NULL_BYTE_ESCAPE = "'||CHAR(0)||'"

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
        "fetch_inserted",
        "install_regexp_functions",
        "load_sqlite_vec",
        "load_spatialite",
        "spatialite_path",
        "spatialite_proj_database",
        "spatialite_metadata",
        "connect_max_retries",
        "connect_retry_backoff_base_seconds",
    }
)

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
