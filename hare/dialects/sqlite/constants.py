import datetime
import re
from collections.abc import Callable
from typing import Any

from hare.dialects.base.connection_option import ConnectionOption
from hare.dialects.base.connection_options import ConnectionOptions
from hare.dialects.enums import ConnectionOptionType
from hare.dialects.sqlite.dialect import SqliteDialect
from hare.utils.patterns import LazyPattern

#: sqlite3.OperationalError's message for a statement binding more parameters than
#: SQLITE_LIMIT_VARIABLE_NUMBER allows.
SQLITE_TOO_MANY_VARIABLES_MESSAGE = "too many SQL variables"
#: sqlite3.OperationalError message fragments for a statement joining more tables than SQLite
#: allows (64 tables in one join, 200 FROM-clause terms).
SQLITE_TOO_MANY_JOINED_TABLES_MESSAGES = ("tables in a join", "too many FROM clause terms")
#: The file name SQLite opens as a private in-memory database instead of a file.
SQLITE_IN_MEMORY_FILENAME = ":memory:"

#: Default PRAGMA values applied on connect unless overridden via connection credentials.
SQLITE_DEFAULT_JOURNAL_MODE = "WAL"
SQLITE_DEFAULT_JOURNAL_SIZE_LIMIT = 16384
SQLITE_DEFAULT_FOREIGN_KEYS = "ON"

#: PRAGMA values SQLite itself treats as "enabled" for a boolean pragma such as foreign_keys.
SQLITE_PRAGMA_ENABLED_VALUES = frozenset({"1", "on", "true", "yes"})

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

#: Lists every row whose foreign key points at a missing parent row.
SQLITE_FOREIGN_KEY_CHECK_SQL = "PRAGMA foreign_key_check"
#: SQLite's LIKE ignores ASCII case by default - switched on, so __contains/__startswith/__endswith
#: match as on Postgres. The case-insensitive lookups wrap both sides in the hare_upper function and
#: don't depend on it.
SQLITE_DEFAULT_CASE_SENSITIVE_LIKE = "ON"

#: Turn SQLite's connection-wide write refusal on for a `Transactions.atomic(
#: read_only=True)` block, and back off before that transaction ends.
SQLITE_ENABLE_QUERY_ONLY_SQL = "PRAGMA query_only = ON"
SQLITE_DISABLE_QUERY_ONLY_SQL = "PRAGMA query_only = OFF"

#: Rows changed by the most recent INSERT/UPDATE/DELETE statement itself - trigger and
#: foreign-key action changes excluded.
SQLITE_LAST_STATEMENT_CHANGES_SQL = "SELECT changes()"

#: sqlite3/aiosqlite can't bind a NUL byte in a query string - substitute it with an
#: expression that concatenates the literal NUL back in at execution time.
SQLITE_NULL_BYTE = "\x00"
SQLITE_NULL_BYTE_ESCAPE = "'||CHAR(0)||'"

#: Prefix SQLite gives the backing index of a UNIQUE/PRIMARY KEY constraint declared inline in a
#: CREATE TABLE body - DROP INDEX against a name with this prefix always fails, only a table
#: rebuild without the constraint can remove it.
SQLITE_AUTOINDEX_PREFIX = "sqlite_autoindex_"

#: The exact, stable message SQLite's own C source uses when a DELETE's native ON DELETE CASCADE
#: recurses past SQLITE_LIMIT_TRIGGER_DEPTH - see SqliteTriggerRecursionLimitError.
SQLITE_TRIGGER_RECURSION_LIMIT_MESSAGE = "too many levels of trigger recursion"

#: Raised for any further statement in a transaction SQLite already rolled back on its own - an
#: interrupted write (statement_timeout, task cancellation) or a failed statement such as a
#: trigger's RAISE(ROLLBACK) ends the whole transaction, savepoints included.
SQLITE_TRANSACTION_ABORTED_MESSAGE = (
    "current transaction is aborted - SQLite rolled back the whole transaction (savepoints included) "
    "when a statement in it failed; exit the transaction block and retry it"
)

#: How long close() waits for the transaction or query holding the connection to finish before
#: closing it anyway.
SQLITE_CLOSE_TIMEOUT_SECONDS = 10

#: aiosqlite's message for any call on a connection that has already been closed.
AIOSQLITE_NO_ACTIVE_CONNECTION_MESSAGE = "no active connection"

#: A bound Decimal is written in fixed-point notation (``0.0000000001``, as Postgres prints a
#: numeric) only while its exponent stays within this many places either side of the point -
#: past that, fixed-point text would grow without bound and Decimal's own scientific text is kept.
SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT = 1000

#: Keyword of a primary key column whose ids are never reused - SQLite keeps the highest id it
#: ever issued for such a table in ``sqlite_sequence``.
SQLITE_AUTOINCREMENT_KEYWORD = "AUTOINCREMENT"
#: Carries a rebuilt table's highest issued id over from the table it replaces: raises the
#: counter the copied rows gave the new table, or creates it when no row was copied.
SQLITE_RAISE_REBUILT_TABLE_SEQUENCE_SQL = (
    "UPDATE sqlite_sequence SET seq = (SELECT MAX(seq) FROM sqlite_sequence WHERE name = {old_table}) "
    "WHERE name = {new_table} AND seq < (SELECT MAX(seq) FROM sqlite_sequence WHERE name = {old_table})"
)
#: The highest id is read in a subquery: a HAVING without GROUP BY needs SQLite 3.39.
SQLITE_COPY_REBUILT_TABLE_SEQUENCE_SQL = (
    "INSERT INTO sqlite_sequence (name, seq) SELECT {new_table}, seq FROM "
    "(SELECT MAX(seq) AS seq FROM sqlite_sequence WHERE name = {old_table}) "
    "WHERE seq IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = {new_table})"
)
#: Renaming a rebuilt table into place with the legacy behavior switched on: the modern rename
#: re-validates every view and trigger of the schema, and one naming the table being replaced
#: fails while that table is briefly missing.
SQLITE_ENABLE_LEGACY_ALTER_TABLE_SQL = "PRAGMA legacy_alter_table = ON"
SQLITE_DISABLE_LEGACY_ALTER_TABLE_SQL = "PRAGMA legacy_alter_table = OFF"

#: Expressions converting a column's stored value to the storage format of the new field when a
#: table rebuild changes the field's type - the conversion a Postgres ``USING column::type`` does.
#: A value the conversion can't read is copied unchanged.
#: A non-zero number becomes true.
SQLITE_NUMBER_TO_BOOLEAN_SQL = (
    "CASE WHEN {column} IS NULL THEN NULL WHEN CAST({column} AS REAL) <> 0 THEN 1 ELSE 0 END"
)
#: The spellings Postgres reads as a boolean.
SQLITE_TEXT_TO_BOOLEAN_SQL = (
    "CASE WHEN lower(trim({column})) IN ('t', 'true', 'y', 'yes', 'on', '1') THEN 1 "
    "WHEN lower(trim({column})) IN ('f', 'false', 'n', 'no', 'off', '0') THEN 0 ELSE {column} END"
)
#: Postgres prints a boolean as text as ``true``/``false``.
SQLITE_BOOLEAN_TO_TEXT_SQL = "CASE WHEN {column} IS NULL THEN NULL WHEN {column} THEN 'true' ELSE 'false' END"
#: A fractional number is rounded to the nearest integer.
SQLITE_NUMBER_TO_INTEGER_SQL = "CASE WHEN {column} IS NULL THEN NULL ELSE CAST(ROUND({column}) AS INTEGER) END"
#: An integer as fixed-point decimal text with the field's number of decimal places.
SQLITE_INTEGER_TO_DECIMAL_SQL = (
    "CASE WHEN {column} IS NULL THEN NULL ELSE CAST(CAST({column} AS INTEGER) AS TEXT){fraction} END"
)
#: A floating-point number as fixed-point decimal text, rounded to the field's decimal places.
SQLITE_FLOAT_TO_DECIMAL_SQL = "CASE WHEN {column} IS NULL THEN NULL ELSE printf('%.{decimal_places}f', {column}) END"
#: The date of a stored datetime - of its UTC instant for an aware one.
SQLITE_DATETIME_TO_DATE_SQL = "COALESCE(date({column}), {column})"
#: Midnight of a stored date - as a UTC instant when time zone support is on.
SQLITE_DATE_TO_AWARE_DATETIME_SQL = "COALESCE(datetime({column}) || '+00:00', {column})"
SQLITE_DATE_TO_NAIVE_DATETIME_SQL = "COALESCE(datetime({column}), {column})"


#: A sanity ceiling, not a protection against catastrophic backtracking - a short pattern can hang
#: re.search() too.
MAX_REGEX_PATTERN_LENGTH = 1000


#: POSIX named character classes with a fixed member list on Postgres (UTF8), as the Python `re`
#: bracket-expression fragment used in place of the "[:name:]" token - "[[:digit:]]+" becomes
#: "[0-9]+".
POSIX_CHARACTER_CLASS_TRANSLATIONS = {
    "digit": "0-9",
    "space": r"\t\n\v\f\r \x85\u2000-\u2006\u2008-\u200a\u2028\u2029\u205f\u3000",
    "cntrl": r"\x00-\x1f\x7f-\x9f",
    "xdigit": "0-9A-Fa-f",
    "blank": r" \t",
}


#: POSIX named character classes spanning the whole Unicode range, as a Python `re` pattern matching
#: one member character: letters, letters or ASCII digits, anything but a control character or a
#: line/paragraph separator, and the same without the spaces.
POSIX_CHARACTER_CLASS_PATTERNS = {
    "alpha": r"[^\W\d_]",
    "alnum": r"[^\W\d_]|[0-9]",
    "print": r"[^\x00-\x1f\x7f-\x9f\u2028\u2029]",
    "graph": r"[^\x00-\x20\x7f-\x9f\u2000-\u2006\u2008-\u200a\u2028\u2029\u205f\u3000]",
}


#: POSIX named character classes whose member lists are collected from the code points below
#: ``POSIX_CLASS_MEMBER_CODE_POINT_LIMIT`` on first use.
POSIX_COLLECTED_CHARACTER_CLASS_NAMES = ("upper", "lower", "punct")


#: POSIX classes defined by case mapping.
POSIX_CASED_CHARACTER_CLASS_NAMES = ("upper", "lower")


#: End of the last Unicode block holding a character with a case mapping or a punctuation/symbol
#: character (Symbols for Legacy Computing).
POSIX_CLASS_MEMBER_CODE_POINT_LIMIT = 0x1FC00


#: Unicode categories of the POSIX ``punct`` class members: punctuation, symbols, other numbers
#: (superscripts, fractions), enclosing marks, format characters and the no-break spaces.
POSIX_PUNCT_CATEGORIES = frozenset(
    ("Pc", "Pd", "Pe", "Pf", "Pi", "Po", "Ps", "Sc", "Sk", "Sm", "So", "No", "Me", "Cf", "Zs")
)


#: Characters Postgres counts as lowercase although they have no one-character uppercase.
POSIX_LOWER_CLASS_EXTRA_CHARACTERS = "\N{LATIN SMALL LETTER SHARP S}"


#: POSIX classes Postgres widens to ``alpha`` in a case-insensitive match.
POSIX_CASE_INSENSITIVE_ALPHA_CLASS_NAMES = ("upper", "lower")


#: How many characters follow the escape letter of a fixed-length character escape (``\x41``,
#: ``\u0041``, ``\U00000041``) - copied verbatim, never read as pattern characters.
REGEX_ESCAPE_ARGUMENT_LENGTHS = {"x": 2, "u": 4, "U": 8}


#: Single-letter escapes standing for one control character.
REGEX_CHARACTER_ESCAPES = {"a": "\a", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


#: Characters that may close the prefix of a ``(?...)`` group - after any inline flag letters.
REGEX_GROUP_PREFIX_TERMINATORS = ":)=!>"


#: Size of the compiled-pattern cache of the SQLite ``REGEXP``/``MATCH`` functions.
REGEX_PATTERN_CACHE_SIZE = 256


#: Length of an ISO date text (``YYYY-MM-DD``) - a stored date, not a datetime or a time.
DATE_TEXT_LENGTH = 10


#: Fixed-width text of a time of day, which sorts chronologically - ``field__time`` on SQLite.
TIME_TEXT_FORMAT = "{0.hour:02d}:{0.minute:02d}:{0.second:02d}.{0.microsecond:06d}"


#: Each EXTRACT() date part to the function the hare_extract_date_part UDF applies to a
#: zone-adjusted datetime - SQLite has no EXTRACT().
DATE_PART_EXTRACTORS: dict[str, Callable[[datetime.datetime], int]] = {
    "YEAR": lambda value: value.year,
    "ISOYEAR": lambda value: value.isocalendar()[0],
    "QUARTER": lambda value: (value.month - 1) // 3 + 1,
    "MONTH": lambda value: value.month,
    "WEEK": lambda value: value.isocalendar()[1],
    # 1 (Sunday) to 7 (Saturday), as Django's week_day.
    "DOW": lambda value: value.isoweekday() % 7 + 1,
    "ISODOW": lambda value: value.isoweekday(),
    "DAY": lambda value: value.day,
    "HOUR": lambda value: value.hour,
    "MINUTE": lambda value: value.minute,
    "SECOND": lambda value: value.second,
    "MICROSECOND": lambda value: value.microsecond,
}


#: Name of the Python UDF registered on every SQLite connection that rewrites a JSON text into a
#: canonical form (sorted keys, no whitespace, integral floats as ints) - backs JSONField's exact
#: and __not lookups, which compare by JSON value like Postgres jsonb does, not by stored text.
SQLITE_JSON_CANONICAL_FUNCTION_NAME = "hare_json_canonical"


#: UDF testing one key the way Postgres jsonb ``?`` does - backs ``has_key``.
SQLITE_JSON_HAS_KEY_FUNCTION_NAME = "hare_json_has_key"


#: UDF testing Postgres jsonb containment (``@>``) - backs ``contains``/``contained_by``.
SQLITE_JSON_CONTAINS_FUNCTION_NAME = "hare_json_contains"


#: UDF testing a key list the way Postgres jsonb ``?&``/``?|`` do - backs ``has_keys``/``has_any_keys``.
SQLITE_JSON_HAS_KEYS_FUNCTION_NAME = "hare_json_has_keys"


#: UDF reading an ISO-8601 date/datetime string the way a JSON ``__filter`` date comparison casts
#: it on Postgres - NULL for any other text.
SQLITE_JSON_DATETIME_FUNCTION_NAME = "hare_json_datetime"


#: ``json_type()`` names of each JSON ``__filter`` comparison type.
SQLITE_JSON_TYPE_NAMES: dict[str, tuple[str, ...]] = {
    "boolean": ("true", "false"),
    "number": ("integer", "real"),
    "string": ("text",),
    "container": ("object", "array"),
}


#: The start of Unix time, the zero of the instant a JSON ``__filter`` aware datetime comparison uses.
SQLITE_JSON_DATETIME_EPOCH = datetime.datetime(1970, 1, 1)


#: Integer range SQLite binds and stores exactly - a JSON number outside it is parsed as a REAL.
SQLITE_INTEGER_MIN = -(2**63)


SQLITE_INTEGER_MAX = 2**63 - 1


#: First SQLite version with ``unhex()``, which a JSON array of hex strings is decoded back to
#: BLOBs with.
SQLITE_UNHEX_MIN_VERSION = (3, 41, 0)


#: Distinct stored/bound time texts whose TimeField collation sort key is kept parsed.
SQLITE_TIME_COLLATION_SORT_KEY_CACHE_SIZE = 4096


#: The only integer size Postgres casts a boolean to - its 32-bit ``integer``.
BOOLEAN_CAST_INTEGER_BITS = 32


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


#: Name of the Python UDF registered on every SQLite connection that gives any stored value's
#: exact decimal text (a double at its shortest round-tripping digits), compared under
#: `SQLITE_DECIMAL_COLLATION_NAME`.
SQLITE_DECIMAL_TEXT_FUNCTION_NAME = "hare_decimal_text"


#: Name of the Python UDF registered on every SQLite connection that tells whether a stored
#: number doesn't fit `DECIMAL(max_digits, decimal_places)` - read as its exact decimal text.
SQLITE_DECIMAL_OVERFLOWS_FUNCTION_NAME = "hare_decimal_overflows"

#: Name of the Python UDF registered on every SQLite connection that gives the text a
#: `DECIMAL(max_digits, decimal_places)` column stores for a value an UPDATE sets it to.
SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME = "hare_decimal_stored_text"


#: The Python functions registered on every connection for date and datetime arithmetic - SQLite
#: stores them as ISO text and has no interval type; its own date functions lose microseconds and
#: offsets.
SQLITE_DATETIME_SHIFT_FUNCTION_NAME = "hare_datetime_shift"


SQLITE_DATE_SHIFT_FUNCTION_NAME = "hare_date_shift"


SQLITE_DATETIME_DIFFERENCE_FUNCTION_NAME = "hare_datetime_difference"


SQLITE_DATE_DIFFERENCE_FUNCTION_NAME = "hare_date_difference"


#: What SQLite's column DEFAULT takes without parentheses: a signed number or a literal.
SQLITE_BARE_DEFAULT_PATTERN = LazyPattern(
    r"\s*(?:[+-]?\s*(?:(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?|0x[0-9a-f]+)|'(?:[^']|'')*'|x'[0-9a-f]*'"
    r"|null|true|false|current_time|current_date|current_timestamp)\s*",
    re.IGNORECASE,
)


#: The SQLite function giving the current date/time in a zone with DST rules, which SQLite's own
#: date functions can't apply - (zone name, "date" or "time").
SQLITE_LOCAL_NOW_FUNCTION_NAME = "hare_local_now"


#: The SQLite collation a TimeField column is compared, ordered and aggregated through - orders
#: times with different offsets the way Postgres orders TIMETZ.
SQLITE_TIME_COLLATION_NAME = "hare_time"

#: A call to one of hare's own SQLite functions or collations in rendered SQL - a condition written
#: into a SQLite constraint or index can't use one: it doesn't exist outside hare's connections.
SQLITE_OWN_FUNCTION_PATTERN = LazyPattern(r'(?<!")\bhare_\w+')

#: SQLite UDF walking a JSON path holding a digit segment, which is an object's key or an array's
#: index, whichever the value at that point is.
SQLITE_JSON_PATH_FUNCTION_NAME = "hare_json_path"


#: Prefix of the SQLite UDFs ``MathFunction`` renders a call to - SQLite's own math functions are
#: an optional build feature.
SQLITE_MATH_FUNCTION_PREFIX = "hare_math_"

#: Prefix of the SQLite UDFs ``TextFunction`` renders a call to where SQLite has no such function,
#: or one with other semantics.
SQLITE_TEXT_FUNCTION_PREFIX = "hare_text_"

#: SQLite's own function of the same semantics, by the Postgres name ``TextFunction`` takes.
SQLITE_NATIVE_TEXT_FUNCTIONS = {"LTRIM": "ltrim", "RTRIM": "rtrim", "REPLACE": "replace", "STRPOS": "instr"}

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

#: Name of the SQLite UDF comparing two JSON values in Postgres's ``jsonb`` order.
SQLITE_JSON_COMPARE_FUNCTION_NAME = "hare_json_compare"

#: Names of the SQLite UDFs giving the byte key ``ORDER BY`` sorts a decimal, time or JSON value by
#: - in the order of its collation, one call per row instead of a comparison per pair of rows.
SQLITE_DECIMAL_SORT_KEY_FUNCTION_NAME = "hare_decimal_sort_key"
SQLITE_TIME_SORT_KEY_FUNCTION_NAME = "hare_time_sort_key"
SQLITE_JSON_SORT_KEY_FUNCTION_NAME = "hare_json_sort_key"
#: The first byte of the sort key of an INTEGER or REAL, a TEXT and a BLOB value - SQLite orders
#: values of different storage classes so.
SQLITE_SORT_KEY_NUMBER_CLASS = b"\x10"
SQLITE_SORT_KEY_TEXT_CLASS = b"\x20"
SQLITE_SORT_KEY_BLOB_CLASS = b"\x30"

#: SQLite's primary result code SQLITE_BUSY - another connection holds a lock the statement needs.
SQLITE_BUSY_RESULT_CODE = 5

#: The bits of an extended SQLite result code that hold its primary result code.
SQLITE_PRIMARY_RESULT_CODE_MASK = 0xFF

#: Now() as a db_default, in the format a DatetimeField value is written in: a " " separator, a
#: 6-digit fraction only when nonzero, and "+00:00" under use_tz=True. 'now' has millisecond
#: precision.
SQLITE_NOW_SQL_TEMPLATE = (
    "(strftime('{text_format}', {moment})"
    " || CASE substr(strftime('%f', {moment}), 4) WHEN '000' THEN ''"
    " ELSE '.' || substr(strftime('%f', {moment}), 4) || '000' END{suffix})"
)
SQLITE_NOW_DATETIME_TEXT_FORMAT = "%Y-%m-%d %H:%M:%S"
SQLITE_NOW_TIME_TEXT_FORMAT = "%H:%M:%S"
SQLITE_NOW_UTC_SQL = SQLITE_NOW_SQL_TEMPLATE.format(
    text_format=SQLITE_NOW_DATETIME_TEXT_FORMAT, moment="'now'", suffix=" || '+00:00'"
)
SQLITE_NOW_LOCAL_SQL = SQLITE_NOW_SQL_TEMPLATE.format(
    text_format=SQLITE_NOW_DATETIME_TEXT_FORMAT, moment="'now', 'localtime'", suffix=""
)
#: SQLite's Now() db_default of a DateField - {moment} as for SQLITE_NOW_SQL_TEMPLATE.
SQLITE_NOW_DATE_SQL_TEMPLATE = "(date({moment}))"
#: SQLite's 'now' shifted by a whole number of minutes (a zone with one constant UTC offset).
SQLITE_NOW_SHIFTED_MOMENT_TEMPLATE = "'now', '{minutes:+d} minutes'"
SQLITE_NOW_LOCAL_MOMENT = "'now', 'localtime'"
SQLITE_NOW_LOCAL_NOW_FUNCTION_SQL_TEMPLATE = "(" + SQLITE_LOCAL_NOW_FUNCTION_NAME + "({zone}, '{value_type}'))"

#: SQLite's RandomHex() db_default: 16 random bytes as 32 lower-case hex digits.
SQLITE_RANDOM_HEX_SQL = "(lower(hex(randomblob(16))))"

#: SQLite (db type keyword, field path, extra kwargs) - same first-match-wins ordering rule as
#: POSTGRESQL_TYPE_MAP.
SQLITE_TYPE_MAP: list[tuple[str, str, dict[str, Any]]] = [
    # hare-orm's own SQLite schema editor declares a plain IntField column as just "INT" (see
    # IntField.SQL_TYPE), not "INTEGER" - "int" has to come after "bigint"/"smallint" (both of
    # which also contain "int" as a substring) to avoid misfiring on those first.
    ("bigint", "hare.fields.data.numeric.BigIntField", {}),
    ("smallint", "hare.fields.data.numeric.SmallIntField", {}),
    ("int", "hare.fields.data.numeric.IntField", {}),
    ("bool", "hare.fields.data.boolean.BooleanField", {}),
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
    ("blob", "hare.fields.data.binary.BinaryField", {}),
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

# The kwargs client construction reads itself - every other kwarg is a PRAGMA name.
# connect_max_retries/connect_retry_backoff_base_seconds are accepted and unused, so a DB_URL shared
# with another backend still works.
NON_PRAGMA_KWARGS = frozenset(
    {
        "connection_name",
        "fetch_inserted",
        "install_regexp_functions",
        "connect_max_retries",
        "connect_retry_backoff_base_seconds",
    }
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

#: Largest Decimal scale SQLite computes an exact remainder for - both operands are scaled to
#: 64-bit integers by ``10 ** scale`` first.
SQLITE_DECIMAL_MOD_MAX_SCALE = 15


#: How a comment is escaped inside the ``/* ... */`` SQLite keeps a table or column comment in.
SQLITE_COMMENT_ESCAPES = str.maketrans(
    {"\x00": "\\0", "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\x1a": "\\Z", "/": "\\/"}
)
