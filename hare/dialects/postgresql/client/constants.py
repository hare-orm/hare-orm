from __future__ import annotations

import re

from hare.lazy_loading.lazy_pattern import LazyPattern

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

#: The TransactionManagementError message for a COMMIT refused because the transaction is aborted.
POSTGRES_ABORTED_TRANSACTION_COMMIT_MESSAGE = (
    "current transaction is aborted - an earlier statement in it failed, so it was rolled back "
    "instead of committed (nothing was committed); roll back to a savepoint around the failing "
    "statement to keep the transaction usable"
)

#: Savepoint SQL for a nested transaction - {name} is always a name hare generated itself.
POSTGRES_SAVEPOINT_NAME_TEMPLATE = "hare_sp_{unique_id}"

#: SQLSTATE class of an invalid authorization specification - a rejected password or an unknown role.
POSTGRES_AUTHORIZATION_SQLSTATE_CLASS = "28"

#: How a transaction pooler refuses a login itself - PgBouncer answers a failed authentication with
#: SQLSTATE 08P01 and this text, not with the server's class 28.
POSTGRESQL_POOLER_LOGIN_REFUSAL_SQLSTATE = "08P01"

POSTGRESQL_POOLER_LOGIN_REFUSAL_TEXT = "authentication failed"

#: The texts of a failure to connect (``PostgresqlConnectErrors``) - ``database`` is the configured
#: database's name, or ``default`` for the server's default one.
POSTGRESQL_AUTHENTICATION_FAILED_MESSAGE = (
    "Authentication failed connecting to {database} database as user {user!r} - check the configured "
    "password/credentials. Exception: {error}"
)

POSTGRESQL_INVALID_CONNECTION_PARAMETER_MESSAGE = (
    "Can't connect to {database} database - one of the configured connection parameters is invalid. Exception: {error}"
)

POSTGRESQL_CONNECTION_FAILED_MESSAGE = "Can't establish connection to database {database}. Exception: {error}"

POSTGRESQL_DEFAULT_DATABASE_CONNECTION_FAILED_MESSAGE = (
    "Can't establish connection to default database. Verify environment PGDATABASE. Exception: {error}"
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

#: Statements resetting a reusable test database to the state of a freshly created one. None of
#: them requests a checkpoint, unlike DROP DATABASE.
POSTGRES_DATABASE_EXISTS_SQL = "SELECT 1 AS database_exists FROM pg_database WHERE datname = $1"

#: Drops a database - and, behind a transaction pooler, the sessions the pooler keeps on it open
#: after its clients leave.
POSTGRES_DROP_DATABASE_SQL = "DROP DATABASE {database}"

POSTGRES_FORCED_DROP_DATABASE_SQL = "DROP DATABASE {database} WITH (FORCE)"

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

#: The pool settings of a client kept on one connection - a session setting lasts for each of its
#: statements.
POSTGRESQL_SINGLE_CONNECTION_POOL_SETTINGS = {"min_size": 1, "max_size": 1}

#: A statement run again past a transaction pooler after its prepared plan went stale (a schema
#: change altering its result) - another text, so the pooler prepares it afresh.
POSTGRESQL_REPLANNED_SQL_TEMPLATE = "{sql} /* hare: replanned {generation} */"

#: The search path a statement runs with - checked once behind a transaction pooler, which must pass
#: the connection's own on to every server connection it lends.
POSTGRESQL_CURRENT_SEARCH_PATH_SQL = "SELECT current_setting('search_path') AS search_path"

#: The interactive client ``hare dbshell`` opens, and the libpq environment variables it reads the
#: settings the arguments don't carry from.
POSTGRES_SHELL_PROGRAM = "psql"

POSTGRES_PASSWORD_ENVIRONMENT_VARIABLE = "PGPASSWORD"  # nosec B105 - an environment variable name

POSTGRES_SHELL_APPLICATION_NAME_VARIABLE = "PGAPPNAME"

POSTGRES_SHELL_OPTIONS_VARIABLE = "PGOPTIONS"

#: The longest statement text whose type (a write reporting a row count, or not) is kept - a longer
#: one, a bulk statement written for its rows, is read each time.
WRITE_STATEMENT_TYPE_CACHED_TEXT_MAX_LENGTH = 4096
