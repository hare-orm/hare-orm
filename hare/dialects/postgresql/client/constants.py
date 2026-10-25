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

#: The name of a nested transaction's savepoint - hare's own, by a unique id.
POSTGRES_SAVEPOINT_NAME_TEMPLATE = "hare_sp_{unique_id}"

#: SQLSTATE class of an invalid authorization specification - a rejected password or an unknown role.
POSTGRES_AUTHORIZATION_SQLSTATE_CLASS = "28"
#: The SQLSTATE class of a server out of a resource - too many clients, no memory or disk left.
POSTGRES_INSUFFICIENT_RESOURCES_SQLSTATE_CLASS = "53"
#: How PostgreSQL refuses an ON CONFLICT target no unique index or constraint covers - SQLSTATE 42P10
#: (invalid column reference) with a message naming the clause.
POSTGRES_MISSING_CONFLICT_TARGET_SQLSTATE = "42P10"

POSTGRES_MISSING_CONFLICT_TARGET_TEXT = "ON CONFLICT"

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
#: The row a NOT NULL or CHECK violation names in its DETAIL - every column's value, a sensitive
#: field's too, up to the end of the message.
POSTGRES_FAILING_ROW_RE = LazyPattern(r"Failing row contains \(.*", re.DOTALL)
#: What the row of ``POSTGRES_FAILING_ROW_RE`` is shown as.
POSTGRES_HIDDEN_FAILING_ROW_TEXT = "Failing row contains (<hidden>)."

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

#: Whether a database of the name exists.
POSTGRES_DATABASE_EXISTS_SQL = "SELECT 1 AS database_exists FROM pg_database WHERE datname = $1"

#: Drops a database - and, behind a transaction pooler, the sessions the pooler keeps on it open
#: after its clients leave.
POSTGRES_DROP_DATABASE_SQL = "DROP DATABASE {database}"

#: Drops a database, the server ending its sessions first.
POSTGRES_FORCED_DROP_DATABASE_SQL = "DROP DATABASE {database} WITH (FORCE)"

#: Ends every other session of the current database.
POSTGRES_TERMINATE_OTHER_DATABASE_SESSIONS_SQL = (
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
)

#: The statements below reset a reusable test database to the state of a freshly created one. None of
#: them requests a checkpoint, unlike DROP DATABASE.
#: The prepared transactions of the current database.
POSTGRES_DATABASE_PREPARED_TRANSACTIONS_SQL = "SELECT gid FROM pg_prepared_xacts WHERE database = current_database()"

#: Rolls a prepared transaction back.
POSTGRES_ROLLBACK_PREPARED_SQL = "ROLLBACK PREPARED {gid_literal}"

#: The schemas the database's user made.
POSTGRES_USER_SCHEMAS_SQL = (
    r"SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'"
)

#: The roles with settings of their own in the current database.
POSTGRES_DATABASE_ROLE_SETTINGS_SQL = (
    "SELECT setrole::regrole::text AS role_name FROM pg_db_role_setting "
    "WHERE setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database()) AND setrole <> 0"
)

#: ``server_version_num`` is ``major * 10000 + minor`` from PostgreSQL 10 on.
POSTGRES_SERVER_VERSION_NUMBER_MAJOR_FACTOR = 10000

#: Drops a schema and everything in it.
POSTGRES_DROP_SCHEMA_CASCADE_SQL = "DROP SCHEMA {schema} CASCADE"

#: Resets the settings of a database.
POSTGRES_RESET_DATABASE_SETTINGS_SQL = "ALTER DATABASE {database} RESET ALL"

#: Resets the settings of a role in a database.
POSTGRES_RESET_DATABASE_ROLE_SETTINGS_SQL = "ALTER ROLE {role} IN DATABASE {database} RESET ALL"

#: The current transaction's id, as the 32-bit id a catalog row's xmin holds.
POSTGRES_CURRENT_TRANSACTION_ID_EXPRESSION = "(pg_current_xact_id()::text::bigint % 4294967296)"

#: The number of rows of a catalog.
POSTGRES_CATALOG_ROW_COUNT_EXPRESSION = "(SELECT count(*) FROM {catalog})"

#: The catalogs of the current database that DDL writes: its own, not the ones the cluster shares, without
#: the statistics ANALYZE writes and the large objects' data.
POSTGRES_DATABASE_CATALOGS_SQL = (
    "SELECT c.oid::regclass::text AS catalog_name FROM pg_class c "
    "WHERE c.relnamespace = 'pg_catalog'::regnamespace AND c.relkind = 'r' AND NOT c.relisshared "
    "AND c.relname NOT IN ('pg_statistic', 'pg_statistic_ext_data', 'pg_largeobject') ORDER BY 1"
)

#: Whether a catalog holds a row a transaction later than the given one wrote.
POSTGRES_CATALOG_CHANGED_SQL = "EXISTS (SELECT FROM {catalog} WHERE age(xmin) < age('{transaction_id}'::xid))"

#: The catalog whose rows a statement changing no definition writes too - TRUNCATE and ALTER SEQUENCE ...
#: RESTART give a relation new storage - told by the fingerprint of its definitions instead.
POSTGRES_STORAGE_WRITTEN_CATALOG = "pg_class"

#: The digest of every relation's definition: pg_class without its storage and statistics columns.
POSTGRES_CLASS_DEFINITIONS_FINGERPRINT_EXPRESSION = (
    "(SELECT md5(coalesce(string_agg(concat_ws('|', oid, relname, relnamespace, reltype, reloftype, relowner, "
    "relam, reltablespace, relhasindex, relpersistence, relkind, relnatts, relchecks, relhasrules, relhastriggers, "
    "relhassubclass, relrowsecurity, relforcerowsecurity, relreplident, relispartition, "
    "pg_get_expr(relpartbound, oid), reloptions::text, relacl::text), ',' ORDER BY oid), '')) FROM pg_class)"
)

#: The start of a test database's comment keeping the schema it holds - a later test process takes the
#: schema from there instead of creating the database again.
POSTGRES_KEPT_TEST_SCHEMA_COMMENT_PREFIX = "hare-kept-test-schema:"

#: The comment of the current database.
POSTGRES_DATABASE_COMMENT_SQL = (
    "SELECT shobj_description(oid, 'pg_database') AS comment FROM pg_database WHERE datname = current_database()"
)

#: Sets a database's comment - a row of a catalog the cluster shares, so no change of the database's schema.
POSTGRES_COMMENT_ON_DATABASE_SQL = "COMMENT ON DATABASE {database} IS {comment}"

#: Whether the database holds data emptying its tables leaves: a materialized view's rows, a foreign
#: table's, a large object.
POSTGRES_DATA_BEYOND_TABLES_SQL = (
    "SELECT EXISTS (SELECT FROM pg_class WHERE relkind IN ('m', 'f')) "
    "OR EXISTS (SELECT FROM pg_largeobject_metadata) AS found"
)

#: The condition of a pg_class row ``c`` (in pg_namespace ``n``) the database's user made: in a user schema,
#: not created by an extension.
POSTGRES_USER_RELATION_CONDITION = (
    r"n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema' AND NOT EXISTS ("
    "SELECT FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"
)

#: Every table the database's user made, quoted with its schema.
POSTGRES_USER_TABLES_SQL = (
    "SELECT format('%I.%I', n.nspname, c.relname) AS table_name FROM pg_class c "  # nosec B608 - constants only
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    f"WHERE c.relkind IN ('r', 'p') AND {POSTGRES_USER_RELATION_CONDITION}"
)

#: Every sequence the database's user made that gave out a value, quoted with its schema.
POSTGRES_USED_SEQUENCES_SQL = (
    "SELECT format('%I.%I', n.nspname, c.relname) AS sequence_name FROM pg_class c "  # nosec B608 - constants only
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE CASE WHEN c.relkind = 'S' THEN pg_sequence_last_value(c.oid) IS NOT NULL END "
    f"AND {POSTGRES_USER_RELATION_CONDITION}"
)

POSTGRES_RESTART_SEQUENCE_SQL = "ALTER SEQUENCE {sequence} RESTART"

#: First server version whose fresh public schema is owned by pg_database_owner with only USAGE
#: granted to PUBLIC - older ones grant PUBLIC both CREATE and USAGE.
POSTGRES_DATABASE_OWNER_PUBLIC_SCHEMA_VERSION_NUMBER = 150000

POSTGRES_CREATE_PUBLIC_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA public AUTHORIZATION pg_database_owner",
    "GRANT USAGE ON SCHEMA public TO PUBLIC",
    "COMMENT ON SCHEMA public IS 'standard public schema'",
)

#: A root certificate file of the TLS settings that can't be read or holds no certificate - asyncpg
#: reported it as an unreachable server (retried), rust_pg as a bare ValueError.
POSTGRES_UNUSABLE_ROOT_CERTIFICATE_MESSAGE = "ssl_root_cert {path!r} cannot be used: {reason}"

#: A LISTEN channel the server can't take - an empty name or one holding a null byte.
POSTGRES_UNUSABLE_CHANNEL_MESSAGE = "{channel!r} is no channel name - it is empty or holds a null byte ('\\x00')"

#: A statement text holding a null byte - the Postgres protocol can't carry one, and the drivers
#: report it as a broken connection (asyncpg) or an encoding failure (rust_pg).
POSTGRES_STATEMENT_NULL_BYTE_MESSAGE = (
    "The SQL statement text contains a null byte ('\\x00'), which PostgreSQL can't receive - pass the "
    "value as a query parameter instead"
)
#: How the server refuses a bound text holding a null byte: a ``text``/``varchar`` value (SQLSTATE
#: 22021, character not in repertoire) and a ``jsonb`` one (22P05, ``\u0000`` untranslatable).
POSTGRES_NULL_CHARACTER_SQLSTATES = frozenset({"22021", "22P05"})
#: The refusal of a bound text holding a null byte.
POSTGRES_VALUE_NULL_BYTE_MESSAGE = "a text value can't contain a null byte ('\\x00') on PostgreSQL"

POSTGRES_CREATE_LEGACY_PUBLIC_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA public",
    "GRANT ALL ON SCHEMA public TO PUBLIC",
    "COMMENT ON SCHEMA public IS 'standard public schema'",
)

#: The pool settings of a client kept on one connection - a session setting lasts for each of its
#: statements.
POSTGRESQL_SINGLE_CONNECTION_POOL_SETTINGS = {"min_size": 1, "max_size": 1}


#: The search path a statement runs with - checked once behind a transaction pooler, which must pass
#: the connection's own on to every server connection it lends.
POSTGRESQL_CURRENT_SEARCH_PATH_SQL = "SELECT current_setting('search_path') AS search_path"
#: The search path for the rest of the transaction - a delete of a row every tenant shares visits
#: each tenant's schema.
POSTGRESQL_SET_TRANSACTION_SEARCH_PATH_SQL = "SELECT set_config('search_path', $1, true) AS search_path"

#: The interactive client ``hare dbshell`` opens, and the libpq environment variables it reads the
#: settings the arguments don't carry from.
POSTGRES_SHELL_PROGRAM = "psql"

POSTGRES_PASSWORD_ENVIRONMENT_VARIABLE = "PGPASSWORD"  # nosec B105 - an environment variable name

POSTGRES_SHELL_APPLICATION_NAME_VARIABLE = "PGAPPNAME"

POSTGRES_SHELL_OPTIONS_VARIABLE = "PGOPTIONS"

#: The longest statement text whose type (a write reporting a row count, or not) is kept - a longer
#: one, a bulk statement written for its rows, is read each time.
WRITE_STATEMENT_TYPE_CACHED_TEXT_MAX_LENGTH = 4096

#: The error of a setup statement of a pool whose client was closed while it opened - opened anew.
POSTGRES_POOL_CLOSED_WHILE_OPENING_MESSAGE = "The connection was closed while its pool was opening"

#: Whether a schema exists - read once by the client of a tenant's schema as it opens its pool.
POSTGRESQL_SCHEMA_EXISTS_SQL = "SELECT 1 AS found FROM pg_catalog.pg_namespace WHERE nspname = $1"
#: The refusal of a tenant scope whose schema doesn't exist - every query would name a missing table.
POSTGRESQL_MISSING_TENANT_SCHEMA_MESSAGE = (
    "The tenant schema {schema!r} doesn't exist - create it with TenantSchemas.create()"
)

#: The start of Python's own error of an int with more digits than it writes as text - the client reports it
#: with ``SQL_LONG_INT_MESSAGE``.
PYTHON_LONG_INT_ERROR_START = "Exceeds the limit ("
#: The text of a PoolTimeoutError - waiting for a connection of a pool ran out of ``pool_acquire_timeout``.
POOL_TIMEOUT_MESSAGE = "Timed out after {seconds}s waiting for a free connection of the pool of {connection_alias!r}"
