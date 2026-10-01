import datetime
import decimal
import re
import uuid
from typing import Any

from hare.dialects.base.connection_option import ConnectionOption
from hare.dialects.base.connection_options import ConnectionOptions
from hare.dialects.enums import ConnectionOptionType, ParameterPosition
from hare.dialects.postgresql.dialect import PostgresqlDialect
from hare.dialects.postgresql.enums import PostgresqlLookup
from hare.query.enums import Lookup
from hare.sql.enums import JsonValueType
from hare.utils.patterns import LazyPattern

#: Default connection pool bounds when not overridden via connection credentials.
DEFAULT_POOL_MINSIZE = 1
DEFAULT_POOL_MAXSIZE = 16

#: Default connect-retry credentials (PostgresqlClient._create_pool_with_retry) - retry
#: disabled by default (0 retries) for backward compatibility with existing callers that expect
#: a connection failure to raise immediately.
DEFAULT_CONNECT_MAX_RETRIES = 0
DEFAULT_CONNECT_RETRY_BACKOFF_BASE_SECONDS = 0.1

#: How often a read-only query is retried when its connection is lost mid-query - a write never is.
#: Off by default.
DEFAULT_READ_QUERY_MAX_RETRIES = 0
DEFAULT_READ_QUERY_RETRY_BACKOFF_BASE_SECONDS = 0.1

#: Sanity ceiling (one day, in seconds) for the ``command_timeout`` credential - not a business
#: limit, just a guard against a typo (e.g. milliseconds passed as seconds) silently disabling
#: the timeout in practice.
MAX_COMMAND_TIMEOUT_SECONDS = 86400.0

#: Sanity ceiling for the ``min_size``/``max_size`` pool credentials - far above any real
#: Postgres ``max_connections``, only a guard against a typo.
MAX_POOL_SIZE = 10000

#: Sanity ceiling (one day, in seconds) for the ``pool_acquire_timeout`` credential.
MAX_POOL_ACQUIRE_TIMEOUT_SECONDS = 86400.0

#: Sanity ceilings for the ``connect_max_retries``/``read_retry_max_retries`` credentials (the
#: backoff doubles on every attempt, so a larger count never finishes in practice) and for their
#: ``*_backoff_base_seconds`` counterparts (one hour).
MAX_CONNECT_RETRIES = 100
MAX_READ_QUERY_RETRIES = 100
MAX_RETRY_BACKOFF_BASE_SECONDS = 3600.0

#: Every Postgres session runs with this ``TimeZone`` - server-side date/time casts, ``CURRENT_DATE``,
#: ``LOCALTIME`` and ``timestamptz::date`` read the session zone, and hare stores UTC instants.
POSTGRES_SESSION_TIME_ZONE = "UTC"
#: The ``server_settings`` key the session time zone is sent under.
POSTGRES_SESSION_TIME_ZONE_SETTING = "TimeZone"
#: ``server_settings`` ``TimeZone`` values (lowercased) accepted as naming UTC itself.
POSTGRES_UTC_TIME_ZONE_NAMES = frozenset(
    {
        "utc",
        "etc/utc",
        "uct",
        "etc/uct",
        "gmt",
        "etc/gmt",
        "gmt0",
        "etc/gmt0",
        "gmt+0",
        "etc/gmt+0",
        "gmt-0",
        "etc/gmt-0",
        "greenwich",
        "etc/greenwich",
        "universal",
        "etc/universal",
        "zulu",
        "etc/zulu",
        "z",
    }
)

#: Valid TCP port range for the ``port`` credential.
MIN_POSTGRES_PORT = 1
MAX_POSTGRES_PORT = 65535

#: Sanity ceiling for the ``statement_cache_size`` credential (both drivers).
MAX_STATEMENT_CACHE_SIZE = 1_000_000

#: The port setting of a PostgreSQL connection.
POSTGRES_PORT_OPTION = ConnectionOption(
    "port", ConnectionOptionType.WHOLE_NUMBER, minimum=MIN_POSTGRES_PORT, maximum=MAX_POSTGRES_PORT
)

#: The settings both PostgreSQL drivers take besides host, port, user, password and database.
POSTGRES_CONNECTION_OPTIONS = ConnectionOptions(
    ConnectionOption("min_size", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=MAX_POOL_SIZE),
    ConnectionOption("max_size", ConnectionOptionType.WHOLE_NUMBER, minimum=1, maximum=MAX_POOL_SIZE),
    ConnectionOption("connect_max_retries", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=MAX_CONNECT_RETRIES),
    ConnectionOption(
        "connect_retry_backoff_base_seconds", ConnectionOptionType.SECONDS, maximum=MAX_RETRY_BACKOFF_BASE_SECONDS
    ),
    ConnectionOption(
        "read_retry_max_retries", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=MAX_READ_QUERY_RETRIES
    ),
    ConnectionOption(
        "read_retry_backoff_base_seconds", ConnectionOptionType.SECONDS, maximum=MAX_RETRY_BACKOFF_BASE_SECONDS
    ),
    ConnectionOption(
        "statement_cache_size", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=MAX_STATEMENT_CACHE_SIZE
    ),
    ConnectionOption(
        "pool_acquire_timeout", ConnectionOptionType.SECONDS, positive=True, maximum=MAX_POOL_ACQUIRE_TIMEOUT_SECONDS
    ),
    ConnectionOption(
        "command_timeout", ConnectionOptionType.SECONDS, positive=True, maximum=MAX_COMMAND_TIMEOUT_SECONDS
    ),
    ConnectionOption("schema", ConnectionOptionType.TEXT),
    ConnectionOption("application_name", ConnectionOptionType.TEXT),
)

#: The values of POSTGRES_CONNECTION_OPTIONS left unset.
POSTGRES_CONNECTION_OPTION_DEFAULTS: dict[str, Any] = {
    "min_size": DEFAULT_POOL_MINSIZE,
    "max_size": DEFAULT_POOL_MAXSIZE,
    "connect_max_retries": DEFAULT_CONNECT_MAX_RETRIES,
    "connect_retry_backoff_base_seconds": DEFAULT_CONNECT_RETRY_BACKOFF_BASE_SECONDS,
    "read_retry_max_retries": DEFAULT_READ_QUERY_MAX_RETRIES,
    "read_retry_backoff_base_seconds": DEFAULT_READ_QUERY_RETRY_BACKOFF_BASE_SECONDS,
    "pool_acquire_timeout": None,
    "command_timeout": None,
    "schema": None,
    "application_name": None,
}

#: First statement of a `Transactions.atomic(read_only=True)` transaction.
POSTGRES_SET_TRANSACTION_READ_ONLY_SQL = "SET TRANSACTION READ ONLY"
#: Per-transaction timeouts for `Transactions.atomic(statement_timeout=...)`, in whole
#: milliseconds.
POSTGRES_SET_LOCAL_STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = {milliseconds}"
POSTGRES_SET_LOCAL_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = {milliseconds}"

#: Sent before the COMMIT of a transaction in which a statement failed - Postgres refuses it with
#: SQLSTATE 25P02 when the transaction is aborted, where it would answer the COMMIT itself with a
#: silent ROLLBACK.
POSTGRES_TRANSACTION_ALIVE_CHECK_SQL = "SELECT 1"
#: The TransactionManagementError message for a COMMIT refused because the transaction is aborted.
POSTGRES_ABORTED_TRANSACTION_COMMIT_MESSAGE = (
    "current transaction is aborted - an earlier statement in it failed, so it was rolled back "
    "instead of committed (nothing was committed); roll back to a savepoint around the failing "
    "statement to keep the transaction usable"
)
#: Savepoint SQL for a nested transaction - {name} is always a name hare generated itself.
POSTGRES_SAVEPOINT_NAME_TEMPLATE = "hare_sp_{unique_id}"
POSTGRES_SAVEPOINT_SQL = "SAVEPOINT {name}"
POSTGRES_RELEASE_SAVEPOINT_SQL = "RELEASE SAVEPOINT {name}"
POSTGRES_ROLLBACK_TO_SAVEPOINT_SQL = "ROLLBACK TO SAVEPOINT {name}"

#: SQLSTATE class of an invalid authorization specification - a rejected password or an unknown role.
POSTGRES_AUTHORIZATION_SQLSTATE_CLASS = "28"

#: Default Postgres server port.
POSTGRES_DEFAULT_PORT = 5432


#: The column types bulk_create(use_copy=True) takes - SQL_TYPE without its size - the same on both
#: drivers: COPY BINARY needs every type declared. Array, range and extension types aren't among
#: them.
COPY_SUPPORTED_SQL_TYPES = frozenset(
    {
        "SMALLINT",
        "INT",
        "INTEGER",
        "BIGINT",
        "REAL",
        "DOUBLE PRECISION",
        "NUMERIC",
        "DECIMAL",
        "BOOL",
        "BOOLEAN",
        "TEXT",
        "VARCHAR",
        "CHAR",
        "CHARACTER VARYING",
        "CHARACTER",
        "UUID",
        "DATE",
        "TIME",
        "TIMETZ",
        "TIMESTAMP",
        "TIMESTAMPTZ",
        "JSON",
        "JSONB",
        "BYTEA",
        "BLOB",
    }
)

#: Statement keywords whose execution reports an affected-row count instead of result rows -
#: unless the statement also has a top-level RETURNING clause.
WRITE_STATEMENT_KEYWORDS = frozenset({"UPDATE", "DELETE", "INSERT", "MERGE"})
#: A RETURNING keyword anywhere in the text - a cheap pre-check before the exact, literal- and
#: CTE-aware check.
RETURNING_CLAUSE_RE = LazyPattern(r"\bRETURNING\b", re.IGNORECASE)
#: The next comment opener, string literal, quoted identifier or dollar-quoted string in a query -
#: text a statement-shape check must not read keywords from. A block comment only matches its
#: opener, since Postgres block comments nest.
SQL_COMMENT_OR_QUOTED_TEXT_RE = LazyPattern(
    r"""
    --[^\n]*
    | /\*
    | (?<![\w$])[Ee]'(?:[^'\\]|\\.|'')*'
    | '(?:[^']|'')*'
    | "(?:[^"]|"")*"
    | (?<![\w$])\$(?P<dollar_quote_tag>(?:[^\W\d]\w*)?)\$[\s\S]*?\$(?P=dollar_quote_tag)\$
    """,
    re.VERBOSE,
)
#: A block comment opener or closer, for tracking nested block comments.
SQL_BLOCK_COMMENT_DELIMITER_RE = LazyPattern(r"/\*|\*/")
#: What a string literal, quoted identifier or dollar-quoted string is replaced with before a
#: statement-shape check - a plain word, so a quoted CTE name still reads as a name.
SQL_QUOTED_TEXT_PLACEHOLDER = " _ "
#: A leading `WITH [RECURSIVE]` CTE introducer.
CTE_INTRODUCER_RE = LazyPattern(r"^\s*WITH\s+(?:RECURSIVE\s+)?", re.IGNORECASE)
#: One parenthesized group with no nested parens of its own - repeatedly substituting this away
#: collapses arbitrarily nested parentheses, one nesting level per pass.
PARENTHESIZED_GROUP_RE = LazyPattern(r"\([^()]*\)")
#: One CTE definition once its body and column list are already gone (collapsed by
#: PARENTHESIZED_GROUP_RE): `name AS [[NOT] MATERIALIZED]`, an optional SEARCH/CYCLE clause and the
#: comma before the next CTE, if any.
CTE_DEFINITION_RE = LazyPattern(
    r"""
    ^\s*\w+\s+AS\b\s*(?:NOT\s+)?(?:MATERIALIZED\b)?
    (?:\s*SEARCH\s+(?:BREADTH|DEPTH)\s+FIRST\s+BY\s+\w+(?:\s*,\s*\w+)*\s+SET\s+\w+)?
    (?:\s*CYCLE\s+\w+(?:\s*,\s*\w+)*\s+SET\s+\w+(?:\s+TO\s+\S+\s+DEFAULT\s+\S+)?\s+USING\s+\w+)?
    \s*,?
    """,
    re.IGNORECASE | re.VERBOSE,
)
#: The query's leading word.
LEADING_WORD_RE = LazyPattern(r"\s*(\w+)")

#: Array element SQL type a large ``__in``/``__not_in`` list with no known field type is bound as,
#: by the Python type of its values - looked up along each value's MRO, so an ``IntEnum`` member
#: binds as a plain integer. ``datetime`` is decided separately (aware vs naive), since it is also a
#: ``date``.
POSTGRES_ARRAY_ELEMENT_TYPE_BY_PYTHON_TYPE: dict[type, str] = {
    bool: "BOOLEAN",
    int: "BIGINT",
    float: "DOUBLE PRECISION",
    decimal.Decimal: "NUMERIC",
    str: "TEXT",
    uuid.UUID: "UUID",
    bytes: "BYTEA",
    datetime.date: "DATE",
    datetime.time: "TIME",
    datetime.timedelta: "INTERVAL",
}
#: Array element SQL type for an integer outside BIGINT's range.
POSTGRES_WIDE_INTEGER_ARRAY_ELEMENT_TYPE = "NUMERIC"
#: Array element SQL types for timezone-aware and naive datetimes.
POSTGRES_AWARE_DATETIME_ARRAY_ELEMENT_TYPE = "TIMESTAMPTZ"
POSTGRES_NAIVE_DATETIME_ARRAY_ELEMENT_TYPE = "TIMESTAMP"
#: BIGINT's value range.
POSTGRES_BIGINT_MIN = -(2**63)
POSTGRES_BIGINT_MAX = 2**63 - 1

#: Connection credential carrying the lease number of a database leased from the reusable test
#: database pool - its db_create()/db_delete() reset the database instead of creating/dropping it.
REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL = "reusable_test_database_lease"

#: Statements resetting a reusable test database to the state of a freshly created one. None of
#: them requests a checkpoint, unlike DROP DATABASE.
POSTGRES_DATABASE_EXISTS_SQL = "SELECT 1 AS database_exists FROM pg_database WHERE datname = $1"
POSTGRES_TERMINATE_OTHER_DATABASE_SESSIONS_SQL = (
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
)
POSTGRES_DATABASE_PREPARED_TRANSACTIONS_SQL = "SELECT gid FROM pg_prepared_xacts WHERE database = current_database()"
POSTGRES_ROLLBACK_PREPARED_SQL = "ROLLBACK PREPARED {gid_literal}"
POSTGRES_USER_SCHEMAS_SQL = (
    r"SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'"
)
POSTGRES_DATABASE_ROLE_SETTINGS_SQL = (
    "SELECT setrole::regrole::text AS role_name FROM pg_db_role_setting "
    "WHERE setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database()) AND setrole <> 0"
)
POSTGRES_SERVER_VERSION_NUMBER_COLUMN = "server_version_number"
POSTGRES_SERVER_VERSION_NUMBER_SQL = (
    f"SELECT current_setting('server_version_num')::int AS {POSTGRES_SERVER_VERSION_NUMBER_COLUMN}"
)
#: ``server_version_num`` is ``major * 10000 + minor`` from PostgreSQL 10 on.
POSTGRES_SERVER_VERSION_NUMBER_MAJOR_FACTOR = 10000
POSTGRES_DROP_SCHEMA_CASCADE_SQL = "DROP SCHEMA {schema} CASCADE"
POSTGRES_RESET_DATABASE_SETTINGS_SQL = "ALTER DATABASE {database} RESET ALL"
POSTGRES_RESET_DATABASE_ROLE_SETTINGS_SQL = "ALTER ROLE {role} IN DATABASE {database} RESET ALL"
#: First server version whose fresh public schema is owned by pg_database_owner with only USAGE
#: granted to PUBLIC - older ones grant PUBLIC both CREATE and USAGE.
POSTGRES_DATABASE_OWNER_PUBLIC_SCHEMA_VERSION_NUMBER = 150000
POSTGRES_CREATE_PUBLIC_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA public AUTHORIZATION pg_database_owner",
    "GRANT USAGE ON SCHEMA public TO PUBLIC",
    "COMMENT ON SCHEMA public IS 'standard public schema'",
)
#: A statement text holding a null byte - the Postgres protocol can't carry one, and the drivers
#: report it as a broken connection (asyncpg) or an encoding failure (rust_pg).
POSTGRES_STATEMENT_NULL_BYTE_MESSAGE = (
    "The SQL statement text contains a null byte ('\\x00'), which PostgreSQL can't receive - pass the "
    "value as a query parameter instead"
)

POSTGRES_CREATE_LEGACY_PUBLIC_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA public",
    "GRANT ALL ON SCHEMA public TO PUBLIC",
    "COMMENT ON SCHEMA public IS 'standard public schema'",
)

#: libpq ``sslmode`` values, all accepted by asyncpg's ``ssl`` connect argument.
POSTGRES_SSL_MODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})
#: ``sslmode`` values the rust_pg driver's ``ssl_mode`` connect argument accepts.
RUST_PG_SSL_MODES = frozenset({"disable", "prefer", "require", "verify-ca", "verify-full"})
#: DB_URL query parameters that all select the TLS mode of a Postgres connection.
POSTGRES_SSL_QUERY_PARAMS = ("ssl", "sslmode", "ssl_mode")

#: The tsquery function per search type, keyed by `SearchType` values as plain strings - the enum's
#: module imports this one.
SEARCH_TYPE_FUNCTIONS: dict[str, str] = {
    "plain": "PLAINTO_TSQUERY",
    "phrase": "PHRASETO_TSQUERY",
    "raw": "TO_TSQUERY",
    "websearch": "WEBSEARCH_TO_TSQUERY",
}

#: The largest finite ``float4`` - the widest magnitude a pgvector ``vector`` element can hold.
FLOAT4_MAX = 3.4028234663852886e38

#: Matches the text form of a one-row slice (``{{1,2}}``) of a multidimensional array, capturing
#: it without its outermost braces (``{1,2}``) - the text form of that row as an array of its own.
ARRAY_OUTER_BRACES_PATTERN = r"^\{(.*)\}$"

#: ``regexp_replace()`` replacement keeping ``ARRAY_OUTER_BRACES_PATTERN``'s captured row text.
ARRAY_OUTER_BRACES_REPLACEMENT = r"\1"

#: Bounds of a 0-indexed ``ArrayItem``/``__item`` position - Postgres array subscripts are 32-bit
#: integers, and a position is rendered one higher (``index + 1``) or counted from the end
#: (``ArrayLength.get_term(...) + index + 1``).
ARRAY_ITEM_INDEX_MIN = -(2**31 - 1)
ARRAY_ITEM_INDEX_MAX = 2**31 - 2

#: pgvector's accepted range of an IVFFlat index's `lists` storage parameter.
IVFFLAT_LISTS_RANGE = (1, 32768)

#: pgvector's accepted range of an HNSW index's `m` storage parameter.
HNSW_M_RANGE = (2, 100)

#: pgvector's accepted range of an HNSW index's `ef_construction` storage parameter - which must
#: also be at least twice `m`.
HNSW_EF_CONSTRUCTION_RANGE = (4, 1000)


#: The output formats of ``EXPLAIN (FORMAT ...)``.
POSTGRESQL_EXPLAIN_FORMATS = frozenset({"TEXT", "JSON", "XML", "YAML"})
#: The boolean options of ``EXPLAIN (...)``.
POSTGRESQL_EXPLAIN_OPTIONS = frozenset(
    {
        "ANALYZE",
        "BUFFERS",
        "COSTS",
        "GENERIC_PLAN",
        "MEMORY",
        "SETTINGS",
        "SERIALIZE",
        "SUMMARY",
        "TIMING",
        "VERBOSE",
        "WAL",
    }
)


#: The type a bare parameter of each Python type is cast to - bool before int, which it subclasses.
POSTGRESQL_PARAMETER_CASTS: tuple[tuple[type, str], ...] = (
    (bool, "boolean"),
    (datetime.datetime, "timestamptz"),
    (datetime.date, "date"),
    (datetime.time, "time"),
    (decimal.Decimal, "numeric"),
    (int, "bigint"),
    (float, "double precision"),
    (uuid.UUID, "uuid"),
)
#: The type a bare number literal is cast to.
POSTGRESQL_NUMBER_LITERAL_TYPES: dict[type, str] = {
    int: "BIGINT",
    float: "FLOAT",
    decimal.Decimal: "NUMERIC",
}
#: The type a literal selected or compared on its own is cast to, before the number types - checked
#: in order: datetime subclasses date.
POSTGRESQL_SELECTED_LITERAL_TYPES: tuple[tuple[type, str], ...] = (
    (datetime.datetime, "TIMESTAMPTZ"),
    (datetime.date, "DATE"),
    (datetime.time, "TIMETZ"),
    (datetime.timedelta, "BIGINT"),
    (bytes, "BYTEA"),
)
#: The type a bare literal argument of a function or an aggregate is cast to.
POSTGRESQL_FUNCTION_ARGUMENT_TYPES: dict[type, str] = {
    **POSTGRESQL_NUMBER_LITERAL_TYPES,
    str: "TEXT",
    datetime.date: "DATE",
    datetime.datetime: "TIMESTAMPTZ",
    datetime.time: "TIMETZ",
}
#: The type a bare literal argument of a text function is cast to.
POSTGRESQL_TEXT_FUNCTION_ARGUMENT_TYPES: dict[type, str] = {int: "INTEGER", str: "TEXT"}
#: The type a bare literal is cast to, by where it stands - a position selected on its own takes
#: POSTGRESQL_SELECTED_LITERAL_TYPES first.
POSTGRESQL_PARAMETER_TYPES_BY_POSITION: dict[ParameterPosition, dict[type, str]] = {
    ParameterPosition.CASE_BRANCH: POSTGRESQL_NUMBER_LITERAL_TYPES,
    ParameterPosition.RAW_SQL: POSTGRESQL_NUMBER_LITERAL_TYPES,
    ParameterPosition.SELECTED_VALUE: POSTGRESQL_NUMBER_LITERAL_TYPES,
    ParameterPosition.COMPARED_VALUE: POSTGRESQL_NUMBER_LITERAL_TYPES,
    ParameterPosition.FUNCTION_ARGUMENT: POSTGRESQL_FUNCTION_ARGUMENT_TYPES,
    ParameterPosition.TEXT_FUNCTION_ARGUMENT: POSTGRESQL_TEXT_FUNCTION_ARGUMENT_TYPES,
}
#: The positions of a literal selected or compared on its own.
POSTGRESQL_SELECTED_LITERAL_POSITIONS = frozenset({ParameterPosition.SELECTED_VALUE, ParameterPosition.COMPARED_VALUE})
#: The type a literal written into a JSON object is cast to, where its value type alone decides it.
POSTGRESQL_JSON_OBJECT_VALUE_TYPES: dict[JsonValueType, str] = {
    JsonValueType.JSON: "JSONB",
    JsonValueType.BINARY: "BYTEA",
    JsonValueType.PLAIN: "TEXT",
}
#: The digest functions ENCODE(...(CONVERT_TO(text)), 'hex') gives the hex text of.
POSTGRESQL_DIGEST_FUNCTIONS = frozenset({"SHA224", "SHA256", "SHA384", "SHA512"})
#: The math functions with a NUMERIC variant only - every argument is cast to it.
POSTGRESQL_NUMERIC_ONLY_MATH_FUNCTIONS = frozenset({"MOD", "LOG"})


POSTGRESQL_DIALECT = PostgresqlDialect()

#: Lookup of the deferrable foreign key constraint behind one ``on_delete=PROTECT`` field: $1 is the
#: quoted (optionally schema-qualified) referencing table, $2 the referenced table, $3 the ordered
#: FK column names. Returns the constraint's schema-qualified, quoted name.
POSTGRESQL_DEFERRABLE_CONSTRAINT_NAME_QUERY = (
    "SELECT format('%I.%I', nsp.nspname, con.conname) AS name "
    "FROM pg_constraint con "
    "JOIN pg_namespace nsp ON nsp.oid = con.connamespace "
    "WHERE con.contype = 'f' "
    "AND con.condeferrable "
    "AND con.conrelid = to_regclass($1) "
    "AND con.confrelid = to_regclass($2) "
    "AND ARRAY("
    "  SELECT att.attname::text"
    "  FROM unnest(con.conkey) WITH ORDINALITY AS key_column(attnum, ord)"
    "  JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = key_column.attnum"
    "  ORDER BY key_column.ord"
    ") = $3::text[]"
)

#: SQLSTATEs of a statement the server aborted because of a concurrent transaction - running the
#: transaction again can succeed: 40001 serialization_failure, 40P01 deadlock_detected.
POSTGRESQL_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})

#: Lower-cased messages PREPARE TRANSACTION fails with when the server has
#: max_prepared_transactions = 0, and when every one of its slots is already taken.
POSTGRESQL_PREPARED_TRANSACTIONS_DISABLED_MESSAGE = "prepared transactions are disabled"
POSTGRESQL_PREPARED_TRANSACTIONS_LIMIT_REACHED_MESSAGE = "maximum number of prepared transactions reached"

#: PostgreSQL's Now() db_default of a DateField/TimeField: the current wall clock in {zone} (a
#: quoted zone name or an INTERVAL offset), a TimeField's with {offset} attached.
POSTGRESQL_NOW_DATE_SQL_TEMPLATE = "((STATEMENT_TIMESTAMP() AT TIME ZONE {zone})::date)"
POSTGRESQL_NOW_TIME_SQL_TEMPLATE = (
    "(((((STATEMENT_TIMESTAMP() AT TIME ZONE {zone})::time)::text || '{offset}'))::timetz)"
)
#: PostgreSQL's Now(): the moment of the statement, as SQLite's and Django's - CURRENT_TIMESTAMP is
#: the start of the transaction.
POSTGRESQL_NOW_SQL = "STATEMENT_TIMESTAMP()"
POSTGRESQL_NOW_OFFSET_ZONE_TEMPLATE = "INTERVAL '{offset}'"
#: The UTC offset a naive time is bound with (use_tz=False).
POSTGRESQL_NAIVE_TIME_OFFSET = "+00:00"

#: PostgreSQL's RandomHex() db_default: the MD5 of a random number, 32 lower-case hex digits.
POSTGRESQL_RANDOM_HEX_SQL = "md5(random()::text)"

#: The constraints of a table ($1) in a schema ($2; the current schema when NULL), by name.
POSTGRESQL_TABLE_CONSTRAINT_NAMES_SQL = (
    "SELECT con.conname AS name FROM pg_catalog.pg_constraint con "
    "JOIN pg_catalog.pg_class rel ON rel.oid = con.conrelid "
    "JOIN pg_catalog.pg_namespace nsp ON nsp.oid = rel.relnamespace "
    "WHERE rel.relname = $1 AND nsp.nspname = COALESCE($2, current_schema())"
)
#: The sequences a table ($1) in a schema ($2; the current schema when NULL) owns - its serial
#: columns' - by name.
POSTGRESQL_TABLE_SEQUENCE_NAMES_SQL = (
    "SELECT seq.relname AS name FROM pg_catalog.pg_class seq "
    "JOIN pg_catalog.pg_sequence sequence_options ON sequence_options.seqrelid = seq.oid "
    "JOIN pg_catalog.pg_depend dep ON dep.objid = seq.oid AND dep.deptype IN ('a', 'i') "
    "JOIN pg_catalog.pg_class rel ON rel.oid = dep.refobjid "
    "JOIN pg_catalog.pg_namespace nsp ON nsp.oid = rel.relnamespace "
    "WHERE rel.relname = $1 AND nsp.nspname = COALESCE($2, current_schema())"
)

#: The foreign keys of other tables referencing a table ($1) in a schema ($2; the current schema
#: when NULL): the referencing table as written in SQL, the constraint's name and its definition -
#: without the copies a partitioned referencing table keeps for its partitions.
POSTGRESQL_INCOMING_FOREIGN_KEYS_SQL = (
    "SELECT con.conrelid::regclass::text AS table_sql, con.conname AS name, "
    "pg_catalog.pg_get_constraintdef(con.oid) AS definition "
    "FROM pg_catalog.pg_constraint con "
    "JOIN pg_catalog.pg_class rel ON rel.oid = con.confrelid "
    "JOIN pg_catalog.pg_namespace nsp ON nsp.oid = rel.relnamespace "
    "WHERE con.contype = 'f' AND con.conparentid = 0 AND con.conrelid <> con.confrelid "
    "AND rel.relname = $1 AND nsp.nspname = COALESCE($2, current_schema()) "
    "ORDER BY 1, 2"
)
#: Moves the sequence of a generated key column past the highest key a table holds: the table as
#: an SQL string literal, the column as one, and both as identifiers.
POSTGRESQL_RESYNC_SEQUENCE_TEMPLATE = (
    "SELECT setval(pg_catalog.pg_get_serial_sequence({table_literal}, {column_literal}), "
    "COALESCE((SELECT MAX({column}) FROM {table}), 0) + 1, false)"
)
#: The suffix of the table a rebuild keeps a table's rows in while the table is created anew.
POSTGRESQL_TABLE_COPY_SUFFIX = "__copy"
#: Creates one partition of a partitioned table.
POSTGRESQL_PARTITION_CREATE_TEMPLATE = "CREATE TABLE {exists}{partition} PARTITION OF {table} {bound}{storage};"

#: The name of a table storage parameter (``WITH (fillfactor = 70)``) - a lower-case identifier,
#: optionally namespaced (``toast.autovacuum_enabled``).
POSTGRESQL_STORAGE_PARAMETER_NAME_PATTERN = LazyPattern(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?")

#: The advisory lock key ``migrate`` holds for its whole run - "hare" read as a 32-bit integer.
POSTGRESQL_MIGRATION_LOCK_KEY = 0x68617265

#: Most tables one query asks "does it hold a row" when a test database is emptied.
POSTGRESQL_CLEAR_TABLES_PROBE_SIZE = 200

#: A path segment naming an array item (``tags__0``, 0-indexed; a negative index counts from the end).
ARRAY_ITEM_PATH_PATTERN = LazyPattern(r"-?\d+")

#: A path segment naming an array slice (``tags__0_2``, the items from 0 up to, not including, 2).
ARRAY_SLICE_PATH_PATTERN = LazyPattern(r"(\d+)_(\d+)")

#: A path segment naming an array's length.
ARRAY_LENGTH_PATH_SEGMENT = "len"

#: Range path segments reading a bound, by the PostgreSQL function giving it.
RANGE_BOUND_PATH_FUNCTIONS = {"startswith": "lower", "endswith": "upper"}

#: Range path segments reading a boolean property, each the PostgreSQL function of the same name.
RANGE_FLAG_PATH_FUNCTIONS = frozenset({"isempty", "lower_inc", "lower_inf", "upper_inc", "upper_inf"})

#: The methods a range field reads a value through - a subclass overriding one reads it in Python.
RANGE_READING_METHOD_NAMES = (
    "from_db_value",
    "to_python",
    "get_range",
    "canonicalize",
    "coerce_bound",
    "get_python_bound",
    "get_python_range",
    "get_infinite_bound",
)

#: A path segment reading a text value without its accents - PostgreSQL's ``unaccent()``.
UNACCENT_PATH_SEGMENT = "unaccent"

#: The extension the trigram lookups and functions come from.
TRIGRAM_EXTENSION = "pg_trgm"

#: The extension ``unaccent()`` comes from.
UNACCENT_EXTENSION = "unaccent"

#: hstore path segments reading every key or value as a text array, by the PostgreSQL function.
HSTORE_ARRAY_PATH_FUNCTIONS = {"keys": "akeys", "values": "avals"}

#: Lookups of an array field whose filter value is a list of elements.
ARRAY_LIST_LOOKUPS = frozenset({Lookup.EXACT, Lookup.NOT, Lookup.CONTAINS, Lookup.CONTAINED_BY, Lookup.OVERLAP})

#: Lookups of a range field whose filter value is a range - ``contains`` also takes one bound value.
RANGE_VALUE_LOOKUPS = frozenset(
    {
        Lookup.EXACT,
        Lookup.NOT,
        Lookup.CONTAINED_BY,
        Lookup.OVERLAP,
        PostgresqlLookup.FULLY_LT,
        PostgresqlLookup.FULLY_GT,
        PostgresqlLookup.NOT_LT,
        PostgresqlLookup.NOT_GT,
        PostgresqlLookup.ADJACENT_TO,
    }
)

#: The PostgreSQL extension giving GiST operator classes to plain scalar types - an
#: ``ExclusionConstraint`` using GiST needs it for a scalar column (``("team", "=")``).
BTREE_GIST_EXTENSION = "btree_gist"

#: PostgreSQL column types ``btree_gist`` gives a GiST operator class, lower-cased and without a
#: length/precision suffix.
BTREE_GIST_SQL_TYPES = frozenset(
    {
        "smallint",
        "int",
        "integer",
        "int2",
        "int4",
        "int8",
        "bigint",
        "serial",
        "bigserial",
        "real",
        "float4",
        "float8",
        "double precision",
        "numeric",
        "decimal",
        "money",
        "oid",
        "char",
        "character",
        "varchar",
        "character varying",
        "text",
        "bytea",
        "bit",
        "varbit",
        "bit varying",
        "bool",
        "boolean",
        "date",
        "time",
        "timetz",
        "time with time zone",
        "time without time zone",
        "timestamp",
        "timestamptz",
        "timestamp with time zone",
        "timestamp without time zone",
        "interval",
        "uuid",
        "inet",
        "cidr",
        "macaddr",
        "macaddr8",
    }
)

#: Postgres (db type keyword, field path, extra kwargs) - checked in order, first match wins, so
#: more specific keywords come before their substrings (e.g. "bigint" before "int").
POSTGRESQL_TYPE_MAP: list[tuple[str, str, dict[str, Any]]] = [
    # Range types first: "daterange" contains "date", and the first match wins. tsrange has no field
    # and falls to the ambiguous TextField.
    ("int4range", "hare.dialects.postgresql.fields.ranges.IntRangeField", {}),
    ("int8range", "hare.dialects.postgresql.fields.ranges.BigIntRangeField", {}),
    ("numrange", "hare.dialects.postgresql.fields.ranges.DecimalRangeField", {}),
    ("daterange", "hare.dialects.postgresql.fields.ranges.DateRangeField", {}),
    ("tstzrange", "hare.dialects.postgresql.fields.ranges.DateTimeRangeField", {}),
    ("uuid", "hare.fields.data.uuids.UUIDField", {}),
    ("boolean", "hare.fields.data.boolean.BooleanField", {}),
    ("smallint", "hare.fields.data.numeric.SmallIntField", {}),
    ("bigint", "hare.fields.data.numeric.BigIntField", {}),
    ("integer", "hare.fields.data.numeric.IntField", {}),
    ("double precision", "hare.fields.data.numeric.FloatField", {}),
    ("real", "hare.fields.data.numeric.FloatField", {}),
    ("numeric", "hare.fields.data.numeric.DecimalField", {"max_digits": 20, "decimal_places": 6}),
    ("timestamp", "hare.fields.data.temporal.DatetimeField", {}),
    ("date", "hare.fields.data.temporal.DateField", {}),
    ("time", "hare.fields.data.temporal.TimeField", {}),
    ("jsonb", "hare.fields.data.json.JSONField", {}),
    ("json", "hare.fields.data.json.JSONField", {}),
    ("bytea", "hare.fields.data.binary.BinaryField", {}),
    # Reports as a plain base type (data_type="tsvector"), not "USER-DEFINED" - unlike
    # geography/vector, which need their own udt_name-keyed branch instead.
    ("tsvector", "hare.dialects.postgresql.fields.search.TSVectorField", {}),
    # A bare "character" (CHAR/bpchar) data_type never reaches this list at all - it's
    # intercepted earlier, by exact-string match, before the generic substring loop runs.
    ("character varying", "hare.fields.data.text.CharField", {"max_length": 255}),
    ("text", "hare.fields.data.text.TextField", {}),
]

#: By the element type's pg_catalog name (udt_name without its "_" prefix) - "int4", not "integer".
#: A multidimensional array reports the same name, so dimensions aren't rebuilt.
POSTGRESQL_ARRAY_ELEMENT_TYPE_MAP: dict[str, tuple[str, dict[str, Any]]] = {
    "int2": ("hare.fields.data.numeric.SmallIntField", {}),
    "int4": ("hare.fields.data.numeric.IntField", {}),
    "int8": ("hare.fields.data.numeric.BigIntField", {}),
    "float4": ("hare.fields.data.numeric.FloatField", {}),
    "float8": ("hare.fields.data.numeric.FloatField", {}),
    "bool": ("hare.fields.data.boolean.BooleanField", {}),
    "text": ("hare.fields.data.text.TextField", {}),
    "varchar": ("hare.fields.data.text.CharField", {"max_length": 255}),
    "bpchar": ("hare.fields.data.text.CharField", {"max_length": 255}),
    "uuid": ("hare.fields.data.uuids.UUIDField", {}),
    "date": ("hare.fields.data.temporal.DateField", {}),
    "timestamptz": ("hare.fields.data.temporal.DatetimeField", {}),
    "jsonb": ("hare.fields.data.json.JSONField", {}),
    "json": ("hare.fields.data.json.JSONField", {}),
    "numeric": ("hare.fields.data.numeric.DecimalField", {"max_digits": 20, "decimal_places": 6}),
    "int4range": ("hare.dialects.postgresql.fields.ranges.IntRangeField", {}),
    "int8range": ("hare.dialects.postgresql.fields.ranges.BigIntRangeField", {}),
    "numrange": ("hare.dialects.postgresql.fields.ranges.DecimalRangeField", {}),
    "daterange": ("hare.dialects.postgresql.fields.ranges.DateRangeField", {}),
    "tstzrange": ("hare.dialects.postgresql.fields.ranges.DateTimeRangeField", {}),
    "citext": ("hare.dialects.postgresql.fields.citext.CitextField", {}),
    "hstore": ("hare.dialects.postgresql.fields.hstore.HStoreField", {}),
    "geography": ("hare.dialects.postgresql.fields.gis.PostGISField", {}),
}

#: Postgres type casts whose quoted default literal (e.g. "'-1.5'::double precision") is a number.
POSTGRESQL_NUMERIC_CAST_TYPES = frozenset({"smallint", "integer", "bigint", "numeric", "real", "double precision"})

#: index_type values whose WITH (...) storage parameters (m/ef_construction/lists) are rendered
#: as constructor kwargs - a NOTE comment flags an index the database reported none for.
POSTGRESQL_TUNED_INDEX_TYPES = frozenset({"hnsw", "ivfflat"})

#: Suffixes of the name Postgres gives an unnamed index (``<table>_<columns>_idx``) or UNIQUE
#: constraint (``<table>_<columns>_key``).
POSTGRESQL_DEFAULT_INDEX_NAME_SUFFIXES = ("idx", "key")

#: Fallback default schema when Postgres's current_schema() reports none.
POSTGRESQL_DEFAULT_SCHEMA = "public"

#: One row when the table ($1) exists in the connection's default schema.
POSTGRESQL_TABLE_EXISTS_SQL = (
    "SELECT 1 FROM pg_catalog.pg_tables WHERE tablename = $1 AND schemaname = current_schema()"
)

#: Primary key columns, in the constraint's own declared column order.
POSTGRESQL_PRIMARY_KEY_COLUMNS_SQL = """
SELECT tc.relname AS table_name, a.attname AS column_name, key_column.position AS ordinal_position
FROM pg_constraint con
JOIN pg_class tc ON tc.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS key_column(attnum, position)
JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = key_column.attnum
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[]) AND con.contype = 'p'
ORDER BY tc.relname, key_column.position
"""

#: Every index, the ones behind UNIQUE constraints and the primary key (is_primary) included; an
#: EXCLUDE constraint's index is left out. Key columns carry their opclass, order flags and
#: collation (only where it differs from the column's); INCLUDE columns are listed apart. term_sqls
#: is each key term as pg_get_indexdef() prints it. nulls_not_distinct is read through to_jsonb() -
#: the column exists from Postgres 15 on.
POSTGRESQL_INDEXES_SQL = """
SELECT tc.relname AS table_name, ic.relname AS index_name, array_agg(a.attname ORDER BY ord.n) AS columns,
       array_agg(oc.opcname ORDER BY ord.n) AS opclasses,
       array_agg(oc.opcdefault ORDER BY ord.n) AS opclass_is_default,
       array_agg(pg_get_indexdef(idx.indexrelid, ord.n::integer, true) ORDER BY ord.n) AS term_sqls,
       array_agg((ord.key_option & 1) = 1 ORDER BY ord.n) AS descending_keys,
       array_agg((ord.key_option & 2) = 2 ORDER BY ord.n) AS nulls_first_keys,
       array_agg(
           CASE WHEN a.attnum IS NOT NULL AND ord.collation_oid <> 0 AND ord.collation_oid <> a.attcollation
               THEN coll.collname END
           ORDER BY ord.n
       ) AS key_collations,
       (
           SELECT array_agg(ia.attname ORDER BY include_key.n)
           FROM unnest(idx.indkey::int2[]) WITH ORDINALITY AS include_key(attnum, n)
           JOIN pg_attribute ia ON ia.attrelid = idx.indrelid AND ia.attnum = include_key.attnum
           WHERE include_key.n > idx.indnkeyatts
       ) AS include_columns,
       idx.indisunique AS is_unique, idx.indisprimary AS is_primary,
       bool_or(COALESCE((to_jsonb(idx) ->> 'indnullsnotdistinct')::boolean, false)) AS nulls_not_distinct,
       am.amname AS index_type,
       pg_get_expr(idx.indpred, idx.indrelid) AS condition_sql,
       pg_get_indexdef(idx.indexrelid) AS index_def,
       ic.reloptions AS storage_parameters,
       COALESCE(con.condeferrable, false) AS is_deferrable,
       COALESCE(con.condeferred, false) AS is_initially_deferred
FROM pg_index idx
JOIN pg_class ic ON ic.oid = idx.indexrelid
JOIN pg_class tc ON tc.oid = idx.indrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
JOIN pg_am am ON am.oid = ic.relam
CROSS JOIN LATERAL unnest(idx.indkey::int2[], idx.indclass::oid[], idx.indoption::int2[], idx.indcollation::oid[])
    WITH ORDINALITY AS ord(attnum, opclassoid, key_option, collation_oid, n)
-- LEFT, not JOIN: an expression index term (e.g. (lower(name))) has attnum = 0, which never
-- matches a pg_attribute row - an inner join would drop that term, or the whole index when every
-- term is an expression. The NULL it leaves in `columns` is how such an index is detected.
LEFT JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = ord.attnum
LEFT JOIN pg_opclass oc ON oc.oid = ord.opclassoid
LEFT JOIN pg_collation coll ON coll.oid = ord.collation_oid
-- The UNIQUE constraint the index backs, for its DEFERRABLE/INITIALLY DEFERRED flags - conrelid
-- keeps out another table's FK constraint whose conindid is this same index.
LEFT JOIN pg_constraint con
    ON con.conindid = idx.indexrelid AND con.conrelid = tc.oid AND con.contype = 'u'
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[]) AND ord.n <= idx.indnkeyatts
    AND idx.indexrelid NOT IN (
        SELECT conindid FROM pg_constraint WHERE conrelid = tc.oid AND contype = 'x'
    )
GROUP BY tc.relname, ic.relname, idx.indisunique, idx.indisprimary, am.amname, idx.indpred, idx.indrelid,
         idx.indexrelid, idx.indkey, idx.indnkeyatts, ic.reloptions, con.condeferrable, con.condeferred
ORDER BY tc.relname, ic.relname
"""

#: Columns in their declared order - information_schema's own type reporting, plus
#: format_type()'s full type text (vector dimensions, array element sizes) and column comments.
POSTGRESQL_COLUMNS_SQL = """
SELECT c.table_name, c.column_name, c.data_type, c.udt_name, c.is_nullable, c.character_maximum_length,
       c.numeric_precision, c.numeric_scale, c.column_default,
       c.is_generated, c.generation_expression, c.is_identity, c.identity_generation,
       col_description(tc.oid, c.ordinal_position) AS description,
       format_type(a.atttypid, a.atttypmod) AS full_type
FROM information_schema.columns c
JOIN pg_namespace ns ON ns.nspname = c.table_schema
JOIN pg_class tc ON tc.relnamespace = ns.oid AND tc.relname = c.table_name
JOIN pg_attribute a
    ON a.attrelid = tc.oid AND a.attname = c.column_name AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.table_schema = $1 AND c.table_name = ANY($2::text[])
ORDER BY c.table_name, c.ordinal_position
"""

#: FOREIGN KEY constraints: local and target columns paired by position, the ON DELETE rule, the
#: target's schema and primary key columns. The copies Postgres adds per partition of a partitioned
#: target are left out.
POSTGRESQL_FOREIGN_KEYS_SQL = """
SELECT tc.relname AS table_name, con.conname,
       array_agg(la.attname ORDER BY ord.n) AS columns,
       array_agg(ra.attname ORDER BY ord.n) AS target_columns,
       target.relname AS target_table, target_ns.nspname AS target_schema,
       CASE con.confdeltype
           WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE'
           WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT'
       END AS delete_rule,
       (
           SELECT array_agg(pa.attname ORDER BY target_key.position)
           FROM pg_constraint pkc
           CROSS JOIN LATERAL unnest(pkc.conkey) WITH ORDINALITY AS target_key(attnum, position)
           JOIN pg_attribute pa ON pa.attrelid = pkc.conrelid AND pa.attnum = target_key.attnum
           WHERE pkc.conrelid = con.confrelid AND pkc.contype = 'p'
       ) AS target_primary_key_columns
FROM pg_constraint con
JOIN pg_class tc ON tc.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
JOIN pg_class target ON target.oid = con.confrelid
JOIN pg_namespace target_ns ON target_ns.oid = target.relnamespace
CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS ord(attnum, target_attnum, n)
JOIN pg_attribute la ON la.attrelid = tc.oid AND la.attnum = ord.attnum
JOIN pg_attribute ra ON ra.attrelid = target.oid AND ra.attnum = ord.target_attnum
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[]) AND con.contype = 'f'
    AND NOT EXISTS (
        SELECT 1 FROM pg_constraint parent_con
        WHERE parent_con.oid = con.conparentid AND parent_con.conrelid = con.conrelid
    )
GROUP BY tc.relname, con.oid, con.conname, target.relname, target_ns.nspname, con.confdeltype, con.confrelid
ORDER BY tc.relname, con.conname
"""

#: The tables of a schema ($1) - plain and partitioned ones, as pg_tables lists them; a partition of
#: a partitioned table only when $2 is true, its parent otherwise stands for it.
POSTGRESQL_TABLE_NAMES_SQL = """
SELECT tc.relname AS tablename
FROM pg_class tc
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
JOIN pg_tables listed ON listed.schemaname = ns.nspname AND listed.tablename = tc.relname
WHERE ns.nspname = $1 AND ($2 OR NOT tc.relispartition)
ORDER BY tc.relname
"""

#: Whether a schema exists.
POSTGRESQL_SCHEMA_EXISTS_SQL = "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1"

#: Table comments, whether the schema is the connection's default one, and storage (persistence,
#: storage parameters, tablespace - NULL for the database's default one, named in
#: default_tablespace) - also tells which requested tables exist.
POSTGRESQL_TABLES_SQL = """
SELECT tc.relname AS table_name, obj_description(tc.oid, 'pg_class') AS description,
       ns.nspname = current_schema() AS is_default_schema,
       tc.relpersistence = 'u' AS unlogged, tc.reloptions AS storage_parameters, ts.spcname AS tablespace,
       (SELECT dts.spcname FROM pg_database db JOIN pg_tablespace dts ON dts.oid = db.dattablespace
        WHERE db.datname = current_database()) AS default_tablespace
FROM pg_class tc
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
LEFT JOIN pg_tablespace ts ON ts.oid = tc.reltablespace
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[])
"""

#: The partitions of partitioned tables ($2) in a schema ($1): one row per partition with the
#: table's strategy, its key columns and their types, the partition's bound, storage parameters
#: and tablespace - a partitioned table without partitions has one row with no partition.
POSTGRESQL_PARTITIONS_SQL = """
SELECT tc.relname AS table_name, pt.partstrat::text AS strategy, pt.partnatts::int AS key_column_count,
       ARRAY(SELECT att.attname::text
             FROM unnest(pt.partattrs::int2[]) WITH ORDINALITY AS key_column(attnum, position)
             JOIN pg_attribute att ON att.attrelid = tc.oid AND att.attnum = key_column.attnum
             ORDER BY key_column.position) AS key_columns,
       ARRAY(SELECT format_type(att.atttypid, att.atttypmod)
             FROM unnest(pt.partattrs::int2[]) WITH ORDINALITY AS key_column(attnum, position)
             JOIN pg_attribute att ON att.attrelid = tc.oid AND att.attnum = key_column.attnum
             ORDER BY key_column.position) AS key_types,
       child.relname AS partition_table, pg_get_expr(child.relpartbound, child.oid) AS bound,
       child.reloptions AS storage_parameters, cts.spcname AS tablespace
FROM pg_partitioned_table pt
JOIN pg_class tc ON tc.oid = pt.partrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
LEFT JOIN pg_inherits inh ON inh.inhparent = tc.oid
LEFT JOIN pg_class child ON child.oid = inh.inhrelid
LEFT JOIN pg_tablespace cts ON cts.oid = child.reltablespace
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[])
ORDER BY tc.relname, child.relname
"""

#: User-defined triggers as Postgres's own canonical CREATE TRIGGER/CREATE FUNCTION text -
#: tgisinternal leaves out the hidden triggers enforcing a FOREIGN KEY constraint.
POSTGRESQL_TRIGGERS_SQL = """
SELECT tc.relname AS table_name, t.tgname AS name, pg_get_triggerdef(t.oid, false) AS trigger_def,
       pg_get_functiondef(p.oid) AS function_def
FROM pg_trigger t
JOIN pg_class tc ON tc.oid = t.tgrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
JOIN pg_proc p ON p.oid = t.tgfoid
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[]) AND NOT t.tgisinternal
ORDER BY tc.relname, t.tgname
"""

#: EXCLUDE and CHECK constraints as pg_get_constraintdef()'s canonical text.
POSTGRESQL_CONSTRAINTS_SQL = """
SELECT tc.relname AS table_name, con.contype::text AS constraint_type, con.conname,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class tc ON tc.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[]) AND con.contype IN ('x', 'c')
ORDER BY tc.relname, con.conname
"""

#: pg_constraint.contype of an EXCLUDE constraint.
POSTGRESQL_EXCLUSION_CONSTRAINT_TYPE = "x"

#: Postgres type names drift compares a column's type by - every spelling Postgres accepts for one
#: of them (``int4``, ``varchar``, ``timestamptz``, ``serial``, ...) mapped to the name
#: ``format_type()`` reports it under. A type outside these values is never compared.
POSTGRESQL_CANONICAL_TYPE_NAMES: dict[str, str] = {
    "int": "integer",
    "int4": "integer",
    "integer": "integer",
    "serial": "integer",
    "serial4": "integer",
    "int2": "smallint",
    "smallint": "smallint",
    "smallserial": "smallint",
    "serial2": "smallint",
    "int8": "bigint",
    "bigint": "bigint",
    "bigserial": "bigint",
    "serial8": "bigint",
    "bool": "boolean",
    "boolean": "boolean",
    "varchar": "character varying",
    "character varying": "character varying",
    "char": "character",
    "character": "character",
    "bpchar": "character",
    "text": "text",
    "decimal": "numeric",
    "numeric": "numeric",
    "float": "double precision",
    "float8": "double precision",
    "double precision": "double precision",
    "float4": "real",
    "real": "real",
    "timestamptz": "timestamp with time zone",
    "timestamp with time zone": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
    "timestamp without time zone": "timestamp without time zone",
    "timetz": "time with time zone",
    "time with time zone": "time with time zone",
    "time": "time without time zone",
    "time without time zone": "time without time zone",
    "date": "date",
    "uuid": "uuid",
    "json": "json",
    "jsonb": "jsonb",
    "bytea": "bytea",
}

#: A Postgres type name in its own parameter-free spelling - ``timestamp(3) with time zone`` keeps
#: its precision apart from the time zone suffix.
POSTGRESQL_TYPE_RE = LazyPattern(
    r"^(?P<name>[a-z_][a-z0-9_ ]*?)\s*(?:\((?P<parameters>[^)]*)\))?"
    r"(?P<time_zone>\s+with(?:out)?\s+time\s+zone)?(?P<array>(?:\s*\[\s*\])*)$"
)

#: The length Postgres gives a ``character`` column declared without one.
POSTGRESQL_DEFAULT_CHARACTER_LENGTH = "1"

#: Moves a table (``{table}``, as a string literal) to the database's default tablespace - named
#: at run time, since ``ALTER TABLE ... SET TABLESPACE`` takes no DEFAULT and the database's
#: default isn't always ``pg_default``.
POSTGRESQL_SET_DEFAULT_TABLESPACE_SQL = """DO $hare$ BEGIN
EXECUTE format('ALTER TABLE %s SET TABLESPACE %I', {table}, (
    SELECT dts.spcname FROM pg_database db JOIN pg_tablespace dts ON dts.oid = db.dattablespace
    WHERE db.datname = current_database()
));
END $hare$"""

#: format_type(atttypid, atttypmod)'s own rendering of a pgvector column, e.g. "vector(768)" -
#: information_schema has no dedicated column for this (vector is an extension type), so the
#: dimension count is parsed out of this text instead.
VECTOR_DIMENSIONS_RE = LazyPattern(r"^vector\((\d+)\)$")

#: format_type()'s text of a numeric[]/varchar[]/char[] column ("numeric(10,2)[]") -
#: information_schema reports no size for an array column.
ARRAY_ELEMENT_NUMERIC_SIZE_RE = LazyPattern(r"^numeric\((\d+),(\d+)\)\[\]$")

ARRAY_ELEMENT_CHAR_LENGTH_RE = LazyPattern(r"^character(?: varying)?\((\d+)\)\[\]$")

EXCLUSION_CONSTRAINT_DEF_RE = LazyPattern(
    r"^EXCLUDE USING (?P<using>\w+) \((?P<expressions>.+?)\)(?: INCLUDE \((?P<include>[^)]*)\))?"
    r"(?: WHERE \((?P<condition>.+)\))?"
    r"(?P<deferrable> DEFERRABLE(?: INITIALLY (?P<initially>DEFERRED|IMMEDIATE))?)?$"
)

EXCLUSION_EXPRESSION_RE = LazyPattern(r'^(?P<field>"[^"]+"|\w+) WITH (?P<operator>[^\s,]+)$')

EXCLUSION_RAW_EXPRESSION_RE = LazyPattern(r"^(?P<expression>.+) WITH (?P<operator>[^\s,]+)$", re.DOTALL)

# pg_get_constraintdef() of a CHECK constraint: "CHECK (<expr>)", with NOT VALID matched apart.
POSTGRES_CHECK_CONSTRAINT_DEF_RE = LazyPattern(r"^CHECK \((?P<expression>.+)\)(?P<not_valid> NOT VALID)?$", re.DOTALL)

POSTGRES_TRIGGERDEF_RE = LazyPattern(
    r"""CREATE\s+(?P<constraint>CONSTRAINT\s+)?TRIGGER\s+"?(?P<name>[\w]+)"?\s+
        (?P<timing>BEFORE|AFTER|INSTEAD\ OF)\s+
        (?P<on>.+?)\s+ON\s+\S+\s+
        (?:FROM\s+(?P<from_table>\S+)\s+)?
        (?:(?P<not_deferrable>NOT\ DEFERRABLE)|DEFERRABLE(?:\s+INITIALLY\s+(?P<initially>IMMEDIATE|DEFERRED))?)?\s*
        FOR\ EACH\ (?P<for_each>ROW|STATEMENT)\s*
        (?:WHEN\s+\((?P<when>.+?)\)\s+)?
        EXECUTE\s+(?:FUNCTION|PROCEDURE)\s+\S+\([^)]*\)\s*;?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

POSTGRES_FUNCTIONDEF_RE = LazyPattern(
    r"LANGUAGE\s+(?P<language>\w+).*?AS\s+\$(?P<tag>\w*)\$(?P<body>.*)\$(?P=tag)\$",
    re.IGNORECASE | re.DOTALL,
)

#: Strips the BEGIN...END the trigger function template wraps the body in.
POSTGRES_FUNCTION_BODY_WRAPPER_RE = LazyPattern(r"\A\s*BEGIN\s*(?P<inner>.*?)\s*END\s*;?\s*\Z", re.DOTALL)

#: An ``EXCLUDE USING`` constraint of a CREATE TABLE.
POSTGRESQL_EXCLUSION_CONSTRAINT_CREATE_TEMPLATE = "CONSTRAINT {name} EXCLUDE USING {using} ({expressions}){where}"
#: The start of a CREATE [UNIQUE] INDEX statement, where CONCURRENTLY goes.
POSTGRESQL_CONCURRENT_INDEX_CREATE_PATTERN = re.compile(r"^(CREATE (?:UNIQUE )?INDEX )")
#: Moves a table into the connection's current schema - SET SCHEMA takes no expression.
POSTGRESQL_MOVE_TABLE_TO_CURRENT_SCHEMA_TEMPLATE = (
    "DO $hare_move_table$ BEGIN EXECUTE format('ALTER TABLE %s SET SCHEMA %I', "
    "{table_literal}, current_schema()); END $hare_move_table$;"
)
POSTGRESQL_TABLE_COMMENT_TEMPLATE = "COMMENT ON TABLE {table} IS {comment};"
POSTGRESQL_COLUMN_COMMENT_TEMPLATE = "COMMENT ON COLUMN {table}.{column} IS {comment};"
#: PostgreSQL has no inline column comment (comments go through COMMENT ON COLUMN), so a generated
#: primary key's column has no {comment} placeholder.
POSTGRESQL_GENERATED_PK_TEMPLATE = "{field_name} {generated_sql}"

#: The language of a trigger's function when its ``Trigger.language`` is None.
POSTGRESQL_DEFAULT_TRIGGER_LANGUAGE = "plpgsql"
