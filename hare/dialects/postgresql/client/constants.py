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

#: The row a NOT NULL or CHECK violation names in its DETAIL - every column's value, a sensitive
#: field's too, up to the end of the message.
POSTGRES_FAILING_ROW_RE = LazyPattern(r"Failing row contains \(.*", re.DOTALL)
#: What the row of ``POSTGRES_FAILING_ROW_RE`` is shown as.
POSTGRES_HIDDEN_FAILING_ROW_TEXT = "Failing row contains (<hidden>)."


#: Drops a test database, the server ending its sessions first - a pooler's, or one a closed event loop
#: left running.
POSTGRES_FORCED_DROP_DATABASE_SQL = "DROP DATABASE {database} WITH (FORCE)"


#: ``server_version_num`` is ``major * 10000 + minor`` from PostgreSQL 10 on.
POSTGRES_SERVER_VERSION_NUMBER_MAJOR_FACTOR = 10000


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

#: The error of a setup statement of a pool whose client was closed while it opened - opened anew.
POSTGRES_POOL_CLOSED_WHILE_OPENING_MESSAGE = "The connection was closed while its pool was opening"

#: The refusal of a tenant scope whose schema doesn't exist - every query would name a missing table.
POSTGRESQL_MISSING_TENANT_SCHEMA_MESSAGE = (
    "The tenant schema {schema!r} doesn't exist - create it with TenantSchemas.create()"
)

#: The start of Python's own error of an int with more digits than it writes as text - the client reports it
#: with ``SQL_LONG_INT_MESSAGE``.
PYTHON_LONG_INT_ERROR_START = "Exceeds the limit ("
#: The text of a PoolTimeoutError - waiting for a connection of a pool ran out of ``pool_acquire_timeout``.
POOL_TIMEOUT_MESSAGE = "Timed out after {seconds}s waiting for a free connection of the pool of {connection_alias!r}"
