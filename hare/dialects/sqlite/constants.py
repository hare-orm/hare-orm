from __future__ import annotations

import re
from typing import Any

from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.base.connection.connection_options import ConnectionOptions
from hare.dialects.enums import ConnectionOptionType
from hare.dialects.sqlite.sqlite_dialect import SqliteDialect
from hare.lazy_loading.lazy_pattern import LazyPattern
from hare.vectors.enums import VectorDistanceType

#: The file name SQLite opens as a private in-memory database instead of a file.
SQLITE_IN_MEMORY_FILENAME = ":memory:"

#: Default PRAGMA values applied on connect unless overridden via connection credentials.
SQLITE_DEFAULT_JOURNAL_MODE = "WAL"
SQLITE_DEFAULT_JOURNAL_SIZE_LIMIT = 16384
SQLITE_DEFAULT_FOREIGN_KEYS = "ON"


#: The connection-level PRAGMAs a connection config may set, besides install_regexp_functions -
#: each value is checked before it is sent (a PRAGMA can't take a bind parameter).
SQLITE_CONNECTION_OPTIONS = ConnectionOptions(
    ConnectionOption(
        "journal_mode", ConnectionOptionType.CHOICE, choices=("DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF")
    ),
    ConnectionOption(
        "synchronous", ConnectionOptionType.CHOICE, choices=("OFF", "NORMAL", "FULL", "EXTRA", "0", "1", "2", "3")
    ),
    ConnectionOption("temp_store", ConnectionOptionType.CHOICE, choices=("DEFAULT", "FILE", "MEMORY", "0", "1", "2")),
    ConnectionOption("locking_mode", ConnectionOptionType.CHOICE, choices=("NORMAL", "EXCLUSIVE")),
    ConnectionOption(
        "auto_vacuum", ConnectionOptionType.CHOICE, choices=("NONE", "FULL", "INCREMENTAL", "0", "1", "2")
    ),
    ConnectionOption("secure_delete", ConnectionOptionType.CHOICE, choices=("ON", "OFF", "FAST", "0", "1")),
    ConnectionOption("foreign_keys", ConnectionOptionType.BOOLEAN),
    ConnectionOption("case_sensitive_like", ConnectionOptionType.BOOLEAN),
    ConnectionOption("recursive_triggers", ConnectionOptionType.BOOLEAN),
    ConnectionOption("automatic_index", ConnectionOptionType.BOOLEAN),
    ConnectionOption("cell_size_check", ConnectionOptionType.BOOLEAN),
    ConnectionOption("defer_foreign_keys", ConnectionOptionType.BOOLEAN),
    ConnectionOption("ignore_check_constraints", ConnectionOptionType.BOOLEAN),
    ConnectionOption("trusted_schema", ConnectionOptionType.BOOLEAN),
    ConnectionOption("reverse_unordered_selects", ConnectionOptionType.BOOLEAN),
    ConnectionOption("journal_size_limit", ConnectionOptionType.WHOLE_NUMBER, minimum=-1, maximum=2**40),
    ConnectionOption("cache_size", ConnectionOptionType.WHOLE_NUMBER, minimum=-(2**31), maximum=2**31 - 1),
    ConnectionOption("busy_timeout", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=86_400_000),
    ConnectionOption("mmap_size", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=2**40),
    ConnectionOption("page_size", ConnectionOptionType.WHOLE_NUMBER, minimum=512, maximum=65536, power_of_two=True),
    ConnectionOption("wal_autocheckpoint", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=2**31 - 1),
    ConnectionOption("threads", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=64),
    ConnectionOption("max_page_count", ConnectionOptionType.WHOLE_NUMBER, minimum=1, maximum=2**32 - 2),
    ConnectionOption("cache_spill", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=2**31 - 1),
)

#: The connection setting that installs hare's REGEXP functions - not a PRAGMA.
SQLITE_REGEXP_OPTION = ConnectionOption("install_regexp_functions", ConnectionOptionType.BOOLEAN)


#: sqlite3/aiosqlite can't bind a NUL byte in a query string - substitute it with an
#: expression that concatenates the literal NUL back in at execution time.
SQLITE_NULL_BYTE = "\x00"


#: Prefix SQLite gives the backing index of a UNIQUE/PRIMARY KEY constraint declared inline in a
#: CREATE TABLE body - DROP INDEX against a name with this prefix always fails, only a table
#: rebuild without the constraint can remove it.
SQLITE_AUTOINDEX_PREFIX = "sqlite_autoindex_"
#: The `origin` PRAGMA index_list gives the index backing a PRIMARY KEY.
SQLITE_PRIMARY_KEY_INDEX_ORIGIN = "pk"


#: Raised for any further statement in a transaction SQLite already rolled back on its own - an
#: interrupted write (statement_timeout, task cancellation) or a failed statement such as a
#: trigger's RAISE(ROLLBACK) ends the whole transaction, savepoints included.
SQLITE_TRANSACTION_ABORTED_MESSAGE = (
    "current transaction is aborted - SQLite rolled back the whole transaction (savepoints included) "
    "when a statement in it failed; exit the transaction block and retry it"
)


#: The aiosqlite driver's name and DB_URL scheme - its client's features are the features of the
#: SQLite library it runs.
SQLITE_AIOSQLITE_DRIVER_NAME = "sqlite+aiosqlite"


#: A bound Decimal is written in fixed-point notation (``0.0000000001``, as Postgres prints a
#: numeric) only while its exponent stays within this many places either side of the point -
#: past that, fixed-point text would grow without bound and Decimal's own scientific text is kept.
SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT = 1000


#: Fixed-width text of a time of day, which sorts chronologically - ``field__time`` on SQLite.
TIME_TEXT_FORMAT = "{0.hour:02d}:{0.minute:02d}:{0.second:02d}.{0.microsecond:06d}"


#: UDF testing one key the way Postgres jsonb ``?`` does - backs ``has_key``.
SQLITE_JSON_HAS_KEY_FUNCTION_NAME = "hare_json_has_key"


#: UDF testing a key list the way Postgres jsonb ``?&``/``?|`` do - backs ``has_keys``/``has_any_keys``.
SQLITE_JSON_HAS_KEYS_FUNCTION_NAME = "hare_json_has_keys"


#: UDF reading an ISO-8601 date/datetime string the way a JSON ``__filter`` date comparison casts
#: it on Postgres - NULL for any other text.
SQLITE_JSON_DATETIME_FUNCTION_NAME = "hare_json_datetime"


#: Integer range SQLite binds and stores exactly - a JSON number outside it is parsed as a REAL.
SQLITE_INTEGER_MIN = -(2**63)


SQLITE_INTEGER_MAX = 2**63 - 1


SQLITE_DIALECT = SqliteDialect()


#: The Python function registered on every connection for EXTRACT - SQLite has neither EXTRACT() nor
#: named time zones.
SQLITE_EXTRACT_FUNCTION_NAME = "hare_extract_date_part"


#: The Python function registered on every connection for UPPER() - SQLite's own folds ASCII only.
SQLITE_UPPER_FUNCTION_NAME = "hare_upper"


#: The same for LOWER().
SQLITE_LOWER_FUNCTION_NAME = "hare_lower"


#: Name of the Python UDF registered on every SQLite connection to back `FloatAsText`/
#: `DecimalAsText` - SQLite's own number-to-text conversion keeps only 15 significant digits,
#: writes `2.0` where Postgres writes `2`, and doesn't pad a decimal to its scale.
SQLITE_NUMBER_TEXT_FUNCTION_NAME = "hare_number_text"


#: Name of the Python collation registered on every SQLite connection to compare a
#: `DecimalField` column's stored text as exact decimals - SQLite has no decimal type, and
#: `CAST(... AS NUMERIC)` turns a value into a double, which keeps only ~15 significant digits.
SQLITE_DECIMAL_COLLATION_NAME = "hare_decimal"


#: Name of the Python UDF registered on every SQLite connection that tells whether a stored
#: number doesn't fit `DECIMAL(max_digits, decimal_places)` - read as its exact decimal text.
SQLITE_DECIMAL_OVERFLOWS_FUNCTION_NAME = "hare_decimal_overflows"


#: The Python functions registered on every connection for date and datetime arithmetic - SQLite
#: stores them as ISO text and has no interval type; its own date functions lose microseconds and
#: offsets.
SQLITE_DATETIME_SHIFT_FUNCTION_NAME = "hare_datetime_shift"


SQLITE_DATE_SHIFT_FUNCTION_NAME = "hare_date_shift"


SQLITE_DATETIME_DIFFERENCE_FUNCTION_NAME = "hare_datetime_difference"


SQLITE_DATE_DIFFERENCE_FUNCTION_NAME = "hare_date_difference"


#: The SQLite function giving the current date/time in a zone with DST rules, which SQLite's own
#: date functions can't apply - (zone name, "date" or "time").
SQLITE_LOCAL_NOW_FUNCTION_NAME = "hare_local_now"


#: The SQLite collation a TimeField column is compared, ordered and aggregated through - orders
#: times with different offsets the way Postgres orders TIMETZ.
SQLITE_TIME_COLLATION_NAME = "hare_time"


#: SQLite UDF walking a JSON path holding a digit segment, which is an object's key or an array's
#: index, whichever the value at that point is.
SQLITE_JSON_PATH_FUNCTION_NAME = "hare_json_path"


#: Prefix of the SQLite UDFs ``MathFunction`` renders a call to - SQLite's own math functions are
#: an optional build feature.
SQLITE_MATH_FUNCTION_PREFIX = "hare_math_"

#: Prefix of the SQLite UDFs ``TextFunction`` renders a call to where SQLite has no such function,
#: or one with other semantics.
SQLITE_TEXT_FUNCTION_PREFIX = "hare_text_"


#: Name of the SQLite UDF ``DateTrunc`` renders a call to - SQLite has no ``DATE_TRUNC()``.
SQLITE_TRUNC_FUNCTION_NAME = "hare_trunc_date_part"

#: Name of the SQLite UDF ``CastTo`` renders a call to - SQLite's own ``CAST`` has other rules.
SQLITE_CAST_FUNCTION_NAME = "hare_cast"

#: Names of the SQLite UDFs ``GreatestLeast`` renders calls to - they skip NULLs, as Postgres does.
SQLITE_GREATEST_FUNCTION_NAME = "hare_greatest"

SQLITE_LEAST_FUNCTION_NAME = "hare_least"

#: SQLite window/aggregate UDF name of each Postgres statistic - SQLite has none of them.
SQLITE_STATISTICS_FUNCTION_NAMES = {
    "STDDEV_POP": "hare_stddev_pop",
    "STDDEV_SAMP": "hare_stddev_samp",
    "VAR_POP": "hare_var_pop",
    "VAR_SAMP": "hare_var_samp",
}

#: Names of the SQLite UDFs writing a value into a JSON object as Postgres's jsonb holds it.
SQLITE_JSON_FLOAT_FUNCTION_NAME = "hare_json_float"

SQLITE_JSON_TIMESTAMP_FUNCTION_NAME = "hare_json_timestamp"

SQLITE_JSON_TIME_FUNCTION_NAME = "hare_json_time"

SQLITE_JSON_BYTES_FUNCTION_NAME = "hare_json_bytes"

#: Name of the SQLite UDF ``DateAsTimestamp`` renders a call to - the stored text of a day's first moment.
SQLITE_DATE_TIMESTAMP_FUNCTION_NAME = "hare_date_timestamp"


#: Names of the SQLite UDFs giving the byte key ``ORDER BY`` sorts a decimal, time or JSON value by
#: - in the order of its collation, one call per row instead of a comparison per pair of rows.
SQLITE_DECIMAL_SORT_KEY_FUNCTION_NAME = "hare_decimal_sort_key"
SQLITE_TIME_SORT_KEY_FUNCTION_NAME = "hare_time_sort_key"
SQLITE_JSON_SORT_KEY_FUNCTION_NAME = "hare_json_sort_key"


#: Now() as a db_default, in the format a DatetimeField value is written in: a " " separator, a
#: 6-digit fraction only when nonzero, and "+00:00" under use_timezone=True. 'now' has millisecond
#: precision.
SQLITE_NOW_SQL_TEMPLATE = (
    "(strftime('{text_format}', {moment})"
    " || CASE substr(strftime('%f', {moment}), 4) WHEN '000' THEN ''"
    " ELSE '.' || substr(strftime('%f', {moment}), 4) || '000' END{suffix})"
)
SQLITE_NOW_DATETIME_TEXT_FORMAT = "%Y-%m-%d %H:%M:%S"
SQLITE_NOW_UTC_SQL = SQLITE_NOW_SQL_TEMPLATE.format(
    text_format=SQLITE_NOW_DATETIME_TEXT_FORMAT, moment="'now'", suffix=" || '+00:00'"
)
SQLITE_NOW_LOCAL_SQL = SQLITE_NOW_SQL_TEMPLATE.format(
    text_format=SQLITE_NOW_DATETIME_TEXT_FORMAT, moment="'now', 'localtime'", suffix=""
)
SQLITE_NOW_LOCAL_NOW_FUNCTION_SQL_TEMPLATE = "(" + SQLITE_LOCAL_NOW_FUNCTION_NAME + "({zone}, '{value_type}'))"


#: SQLite (db type keyword, field path, extra kwargs) - same first-match-wins ordering rule as
#: POSTGRESQL_TYPE_MAP.
SQLITE_TYPE_MAP: list[tuple[str, str, dict[str, Any]]] = [
    # hare-orm's own SQLite schema editor declares a plain IntField column as just "INT" (see
    # IntField.SQL_TYPE), not "INTEGER" - "int" has to come after "bigint"/"smallint" (both of
    # which also contain "int" as a substring) to avoid misfiring on those first.
    ("bigint", "hare.fields.data.numeric.BigIntField", {}),
    ("smallint", "hare.fields.data.numeric.SmallIntField", {}),
    ("int", "hare.fields.data.numeric.IntField", {}),
    ("bool", "hare.fields.data.boolean_field.BooleanField", {}),
    ("real", "hare.fields.data.numeric.FloatField", {}),
    ("double", "hare.fields.data.numeric.FloatField", {}),
    ("float", "hare.fields.data.numeric.FloatField", {}),
    ("numeric", "hare.fields.data.numeric.DecimalField", {"max_digits": 20, "decimal_places": 6}),
    ("decimal", "hare.fields.data.numeric.DecimalField", {"max_digits": 20, "decimal_places": 6}),
    ("timestamp", "hare.fields.data.temporal.DatetimeField", {}),
    ("datetime", "hare.fields.data.temporal.DatetimeField", {}),
    ("date", "hare.fields.data.temporal.DateField", {}),
    ("time", "hare.fields.data.temporal.TimeField", {}),
    ("json", "hare.fields.data.json.JSONField", {}),
    ("blob", "hare.fields.data.binary_field.BinaryField", {}),
    # "varchar" must come before the bare "char" entry - "char" is a substring of "varchar" too,
    # so the more specific keyword has to be checked first (first match wins).
    ("varchar", "hare.fields.data.text.CharField", {"max_length": 255}),
    ("char", "hare.fields.data.text.CharField", {"max_length": 255}),
    ("text", "hare.fields.data.text.TextField", {}),
]

#: SQLite's one namespace of a plain (non-ATTACHed) database.
SQLITE_MAIN_SCHEMA = "main"

SQLITE_TABLE_EXISTS_SQL = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"

#: An identifier in SQLite's literal CREATE TABLE text, in each quoting SQLite accepts - double
#: quotes, backticks, square brackets (a doubled quote character inside stands for one) - or bare.
SQLITE_IDENTIFIER_PATTERN = r'"(?:[^"]|"")+"|`(?:[^`]|``)+`|\[[^\]]+\]|\w+'

#: A table-level ``CONSTRAINT "name" UNIQUE (...)`` clause of SQLite's literal CREATE TABLE text -
#: its backing index is an unnamed ``sqlite_autoindex_*``, so the name is read from here.
SQLITE_TABLE_UNIQUE_CONSTRAINT_RE = LazyPattern(
    rf"\bCONSTRAINT\s+(?P<name>{SQLITE_IDENTIFIER_PATTERN})\s+UNIQUE\s*\((?P<columns>[^)]*)\)", re.IGNORECASE
)

#: SQLite's column type affinity rules, checked in order against the declared type's upper-cased
#: text: the first rule one of whose substrings the type contains names its affinity. A type no
#: rule matches has NUMERIC affinity, an empty one BLOB.
SQLITE_TYPE_AFFINITY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("INT",), "INTEGER"),
    (("CHAR", "CLOB", "TEXT"), "TEXT"),
    (("BLOB",), "BLOB"),
    (("REAL", "FLOA", "DOUB"), "REAL"),  # codespell:ignore
)

SQLITE_DEFAULT_TYPE_AFFINITY = "NUMERIC"

SQLITE_EMPTY_TYPE_AFFINITY = "BLOB"

#: Column types an earlier hare release declared for a field whose type is now the key - a column
#: created with one of them is not reported.
SQLITE_EARLIER_COLUMN_TYPES: dict[str, tuple[str, ...]] = {"JSON_TEXT": ("JSON",)}

#: The table options after the column list of a CREATE TABLE text as ``sqlite_master`` keeps it -
#: ``WITHOUT ROWID`` and ``STRICT``, comma-separated.
SQLITE_TABLE_OPTIONS_PATTERN = LazyPattern(
    r"\)\s*(?P<options>(?:WITHOUT\s+ROWID|STRICT)(?:\s*,\s*(?:WITHOUT\s+ROWID|STRICT))*)\s*;?\s*$",
    re.IGNORECASE,
)


#: A declared SQLite type string can carry its own size, e.g. "VARCHAR(255)" or
#: "DECIMAL(10,2)" - SQLite has no separate metadata for this (its columns are dynamically
#: typed), so the declared type string itself is the only place to read it from.
SQLITE_SIZE_RE = LazyPattern(r"\((\d+)(?:,\s*(\d+))?\)")

SQLITE_INDEX_ON_RE = LazyPattern(rf"\bON\s+(?:{SQLITE_IDENTIFIER_PATTERN}|[\w.]+)\s*\(", re.IGNORECASE)

SQLITE_INDEX_WHERE_RE = LazyPattern(r"^\s*WHERE\s+(?P<condition>.+?)\s*;?\s*$", re.IGNORECASE | re.DOTALL)

#: An index term suffix Index(*expressions) can't carry - it wraps every term in parens.
SQLITE_INDEX_TERM_MODIFIER_RE = LazyPattern(r"\s(?:ASC|DESC)\s*$|\bCOLLATE\b", re.IGNORECASE)

SQLITE_INDEX_TERM_ORDER_RE = LazyPattern(r"\s(?P<direction>ASC|DESC)\s*$", re.IGNORECASE)

SQLITE_INDEX_TERM_COLLATE_RE = LazyPattern(r"\bCOLLATE\b", re.IGNORECASE)

SQLITE_TRIGGERDEF_RE = LazyPattern(
    rf"""CREATE\s+TRIGGER\s+(?:IF\ NOT\ EXISTS\s+)?(?P<name>{SQLITE_IDENTIFIER_PATTERN})\s+
        (?P<timing>BEFORE|AFTER|INSTEAD\ OF)\s+
        (?P<on>.+?)\s+ON\s+(?:{SQLITE_IDENTIFIER_PATTERN})\s*
        (?:FOR\ EACH\ ROW\s*)?
        (?:WHEN\s*\((?P<when>.+?)\)\s*)?
        BEGIN\s*(?P<body>.*?)\s*END\s*;?\s*$""",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)

SQLITE_CHECK_CONSTRAINT_HEADER_RE = LazyPattern(
    rf"(?:CONSTRAINT\s+(?P<name>{SQLITE_IDENTIFIER_PATTERN})\s+)?CHECK\s*\(",
    re.IGNORECASE,
)


#: The connection settings loading the SpatiaLite extension - not PRAGMAs: whether to load it, the
#: library (a name the system's loader finds, or a path), and PROJ's ``proj.db`` its SRID
#: transformations read.
SQLITE_SPATIAL_EXTENSION_OPTION = ConnectionOption("load_spatialite", ConnectionOptionType.BOOLEAN)
#: The access method of a ``SpatialiteIndex`` as hare names it - its R*Tree.
SPATIALITE_INDEX_TYPE = "RTREE"
#: The column a statement creating a spatial index answers in - 1 when it did its part.
SPATIALITE_INDEX_RESULT_COLUMN = "spatialite_result"
#: The table of the spatial metadata registering geometry columns.
SPATIALITE_GEOMETRY_COLUMNS_TABLE_NAME = "geometry_columns"
#: The columns of a table with a spatial index - read from the spatial metadata.
SPATIALITE_INDEXED_COLUMNS_SQL = (
    "SELECT f_geometry_column FROM geometry_columns WHERE lower(f_table_name) = lower(?) "
    "AND spatial_index_enabled = 1 ORDER BY f_geometry_column"
)
#: The prefixes of the names of the triggers SpatiaLite keeps a registered column and its spatial
#: index with - ``<prefix>_<table>_<column>``.
SPATIALITE_TRIGGER_NAME_PREFIXES = ("ggi", "ggu", "gid", "gii", "giu", "tmd", "tmi", "tmu")
#: The tables SpatiaLite's spatial metadata always has - a database holding all of them has the
#: metadata, whose tables are no model's.
SPATIALITE_METADATA_MARKER_TABLE_NAMES = frozenset({"spatial_ref_sys", "geometry_columns", "spatialite_history"})
#: The tables of SpatiaLite's spatial metadata - its virtual tables are left out by being virtual.
SPATIALITE_METADATA_TABLE_NAMES = frozenset(
    {
        "data_licenses",
        "geometry_columns",
        "geometry_columns_auth",
        "geometry_columns_field_infos",
        "geometry_columns_statistics",
        "geometry_columns_time",
        "spatial_ref_sys",
        "spatial_ref_sys_aux",
        "spatialite_history",
        "sql_statements_log",
        "views_geometry_columns",
        "views_geometry_columns_auth",
        "views_geometry_columns_field_infos",
        "views_geometry_columns_statistics",
        "virts_geometry_columns",
        "virts_geometry_columns_auth",
        "virts_geometry_columns_field_infos",
        "virts_geometry_columns_statistics",
    }
)

#: The connection setting loading the sqlite-vec extension - not a PRAGMA.
SQLITE_VECTOR_EXTENSION_OPTION = ConnectionOption("load_sqlite_vec", ConnectionOptionType.BOOLEAN)
#: The SQLite UDF giving the negative inner product of two float32 vectors - sqlite-vec has none.
SQLITE_VECTOR_NEGATIVE_INNER_PRODUCT_FUNCTION_NAME = "hare_vector_negative_inner_product"
#: The function computing each vector distance.
SQLITE_VECTOR_DISTANCE_FUNCTION_NAMES = {
    VectorDistanceType.L2: "vec_distance_l2",
    VectorDistanceType.COSINE: "vec_distance_cosine",
    VectorDistanceType.NEGATIVE_INNER_PRODUCT: SQLITE_VECTOR_NEGATIVE_INNER_PRODUCT_FUNCTION_NAME,
}

#: The FTS5 table options of an index kept in step with its table, its rows keyed by the table's
#: integer primary key.
SQLITE_FULL_TEXT_CONTENT_OPTION = "content"
SQLITE_FULL_TEXT_CONTENT_ROWID_OPTION = "content_rowid"
SQLITE_FULL_TEXT_TOKENIZE_OPTION = "tokenize"
#: The suffix of the name of each trigger keeping an FTS5 index in step with its table.
SQLITE_FULL_TEXT_INSERT_TRIGGER_SUFFIX = "__insert"
SQLITE_FULL_TEXT_DELETE_TRIGGER_SUFFIX = "__delete"
SQLITE_FULL_TEXT_UPDATE_TRIGGER_SUFFIX = "__update"
#: The FTS5 module name, as ``CREATE VIRTUAL TABLE ... USING`` names it.
SQLITE_FULL_TEXT_MODULE = "fts5"
#: The ``CREATE VIRTUAL TABLE ... USING fts5(...)`` text of an FTS5 table as ``sqlite_master``
#: keeps it - its arguments.
SQLITE_FULL_TEXT_TABLE_PATTERN = LazyPattern(
    r"^\s*CREATE\s+VIRTUAL\s+TABLE\s+.+?\s+USING\s+fts5\s*\((?P<arguments>.*)\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
#: One argument of ``USING fts5(...)``: an option (``content='posts'``) or a column, with what
#: follows its name (``UNINDEXED``).
SQLITE_FULL_TEXT_ARGUMENT_PATTERN = LazyPattern(
    rf"""\s*(?:(?P<option>\w+)\s*=\s*(?P<value>'(?:[^']|'')*'|"(?:[^"]|"")*"|[^,]*?)
    |(?P<column>{SQLITE_IDENTIFIER_PATTERN})(?P<modifiers>[^,]*?))\s*(?:,|$)""",
    re.VERBOSE,
)
#: The virtual tables of a database, with their CREATE text - neither they nor the shadow tables
#: keeping their data are a model's.
SQLITE_VIRTUAL_TABLES_SQL = "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL%'"
