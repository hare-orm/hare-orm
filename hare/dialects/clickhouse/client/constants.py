from __future__ import annotations

import datetime
import ipaddress
import re
import uuid
from decimal import Decimal

#: A read grouped ``WITH TOTALS`` - as the dialect writes the modifier; its totals row is read with its rows.
CLICKHOUSE_WITH_TOTALS_PATTERN = re.compile(r"\sWITH TOTALS\b")
#: A write of stored rows - an UPDATE or a DELETE, a mutation or a lightweight one; the server reports no
#: count of the rows it changes.
CLICKHOUSE_ROW_WRITE_PATTERN = re.compile(
    r"\s*(?:ALTER\s+TABLE\s+.+?\s+(?:UPDATE|DELETE)\s|UPDATE\s|DELETE\s+FROM\s)", re.IGNORECASE | re.DOTALL
)
#: The mutations a failed write names - ``... during execution of mutation 'mutation_9.txt' ...`` or
#: ``... of mutations 'mutation_10.txt, mutation_9.txt' ...``.
CLICKHOUSE_FAILED_MUTATIONS_PATTERN = re.compile(r"execution of mutations? '([^']+)'")

#: Stops failed mutations of a table - one left would fail every later mutation of the table, and
#: could still change its rows once the write that made it has raised; only a mutation that has failed
#: is stopped (``{0}``: ``ON CLUSTER``,
#: ``{1}``: the table's name, ``{2}``: the mutations' ids, as literals).
CLICKHOUSE_KILL_MUTATIONS_TEMPLATE = (
    "KILL MUTATION{on_cluster} WHERE database = {database} AND table = {table} AND mutation_id IN ({ids}) "
    "AND latest_fail_reason != ''"
)

#: The ClickHouse error code of a session another request of it still holds - a statement the client
#: stopped waiting for runs on in the session of its transaction.
CLICKHOUSE_SESSION_IS_LOCKED_ERROR_CODE = 373
#: How long a rollback waits for its session held by such a statement - the server's own session
#: timeout, after which the session ends with its transaction anyway.
CLICKHOUSE_LOCKED_SESSION_ROLLBACK_SECONDS = 60
#: How often a rollback asks again for its session held by such a statement.
CLICKHOUSE_LOCKED_SESSION_RETRY_SECONDS = 0.05

#: The ClickHouse error codes of a statement against the data - a violated CHECK constraint.
CLICKHOUSE_INTEGRITY_ERROR_CODES = frozenset({469})
#: The ClickHouse error code of a value of a type it can't convert - NULL written into a NOT NULL
#: column among them, told apart by its message.
CLICKHOUSE_CANNOT_CONVERT_TYPE_ERROR_CODE = 70
CLICKHOUSE_NULL_INTO_NOT_NULL_MESSAGE = "Cannot convert NULL to a non-nullable type"

#: The ClickHouse error code of a TTL the server keeps none by.
CLICKHOUSE_BAD_TTL_EXPRESSION_ERROR_CODE = 450
#: What a TTL of a nullable column takes - the server keeps no TTL of a value that may be NULL.
CLICKHOUSE_NULLABLE_TTL_EXPRESSION_HINT = (
    "a TTL is of a moment that is never NULL - the column is nullable: assumeNotNull(seen) + INTERVAL 30 DAY, or "
    "ifNull(seen, now()) + INTERVAL 30 DAY"
)
#: How the server names a nullable type in an error.
CLICKHOUSE_NULLABLE_TYPE_MARK = "Nullable("

#: The server setting stopping a statement that runs longer, in seconds.
CLICKHOUSE_MAX_EXECUTION_TIME_SETTING = "max_execution_time"
#: How much longer than the server's limit of a statement the HTTP client waits for its answer.
CLICKHOUSE_HTTP_RECEIVE_TIMEOUT_MARGIN_SECONDS = 1
#: The ClickHouse error code of a statement the server can't run yet - in a transaction, one of a
#: table or a type of query transactions don't take.
CLICKHOUSE_NOT_IMPLEMENTED_ERROR_CODE = 48
#: A transaction's statement met a part another transaction holds - retrying the transaction can succeed.
CLICKHOUSE_SERIALIZATION_ERROR_CODE = 650
#: The failure of a mutation, wrapping the error it met - a serialization error by this text.
CLICKHOUSE_UNFINISHED_ERROR_CODE = 341
CLICKHOUSE_SERIALIZATION_ERROR_MESSAGE = "Serialization error"
#: The part of that error's message for a correlated subquery reading a column of a table joined to the
#: query around it.
CLICKHOUSE_JOINED_CORRELATED_COLUMN_MESSAGE = "can't find correlated column"
#: What the refusal of such a subquery adds to the server's message.
CLICKHOUSE_JOINED_CORRELATED_COLUMN_HINT = (
    "ClickHouse correlates a subquery by the columns of the outer query's own table only - an "
    "OuterReference across a relation (OuterReference('book__author_id')) reads a joined one; reach "
    "the relation inside the subquery instead (Author.objects.filter(books__id=OuterReference('book_id')))"
)

#: The ClickHouse error codes of a connection that can't be used: the server refused or dropped it.
CLICKHOUSE_CONNECTION_ERROR_CODES = frozenset({209, 210})

#: The ClickHouse error codes of refused credentials - a password required or wrong. No retry fixes
#: them: a ConfigurationError, as on PostgreSQL.
CLICKHOUSE_AUTHENTICATION_ERROR_CODES = frozenset({194, 516})
CLICKHOUSE_AUTHENTICATION_FAILED_MESSAGE = (
    "Authentication failed connecting to ClickHouse as user {user!r} - check the configured password/credentials. "
    "Exception: {error}"
)

#: The ClickHouse error code of a database that doesn't exist, and how the server names it - the
#: connection's own database missing is a ConfigurationError, as on PostgreSQL.
CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODE = 81
CLICKHOUSE_UNKNOWN_DATABASE_TEXT = "Database {database} does not exist"
CLICKHOUSE_MISSING_DATABASE_MESSAGE = "Can't use database {database} - it doesn't exist. Exception: {error}"

#: Where a ClickHouse error message names its error code - ``Code: 469.``.
CLICKHOUSE_ERROR_CODE_PREFIX = "Code: "

#: The arguments a client takes besides its connection settings - every other one must be a setting.
CLICKHOUSE_CLIENT_BASE_SETTINGS = frozenset({"connection_alias"})

#: The database of a connection naming none.
CLICKHOUSE_DEFAULT_DATABASE = "default"
#: The values of the connection settings left unset.
CLICKHOUSE_CONNECTION_OPTION_DEFAULTS = {
    "secure": False,
    "native_port": None,
    "compress": True,
    "connect_timeout": 10,
    "send_receive_timeout": 300,
    "cluster": None,
    "async_insert": False,
    "wait_for_async_insert": True,
    "transactions": False,
    "keeper_hosts": None,
}

#: The interactive client ``hare dbshell`` opens - it speaks ClickHouse's native protocol, on its own
#: port (``native_port``; the client's default without it), not the HTTP port.
CLICKHOUSE_SHELL_PROGRAM = "clickhouse-client"

CLICKHOUSE_SHELL_PASSWORD_VARIABLE = "CLICKHOUSE_PASSWORD"  # nosec B105 - an environment variable name

#: What may come before a statement's first word: spaces, opening brackets and comments.
CLICKHOUSE_STATEMENT_PREFIX_PATTERN = re.compile(r"(?:\s+|\(|--[^\n]*|/\*.*?\*/)*", re.DOTALL)

#: The first words of the statements that return rows - every other statement is a command.
CLICKHOUSE_ROW_RETURNING_KEYWORDS = frozenset({"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "EXISTS"})

#: The types of the values a driver's binary insert writes as they are - a column holding no other
#: is passed to it untouched. A ``datetime`` is not among them: ``type()`` of one is not ``date``.
CLICKHOUSE_INSERTED_AS_IS_TYPES = frozenset({int, float, bool, type(None), Decimal, uuid.UUID, datetime.date})
#: The types of the values of a column of mixed types the insert writes as they are - a date is
#: not among them: each is checked to be one ClickHouse stores.
CLICKHOUSE_INSERTED_UNCHANGED_VALUE_TYPES = frozenset(
    {int, float, bool, type(None), Decimal, uuid.UUID, ipaddress.IPv4Address, ipaddress.IPv6Address}
)

#: The types of a column of moments, each written in UTC.
CLICKHOUSE_INSERTED_MOMENT_COLUMN_TYPES = frozenset({datetime.datetime, type(None)})

#: The types of a column of text.
CLICKHOUSE_INSERTED_TEXT_COLUMN_TYPES = frozenset({str, type(None)})

#: The most statements whose placeholder layout a ClickHouse client keeps - the statements of an
#: application's queries are few; a raw statement of ever different text must not grow it for good.
CLICKHOUSE_QUERY_TEMPLATE_CACHE_MAX_SIZE = 4096

#: How many rows a batch of a streamed query holds when the reader names no number.
CLICKHOUSE_STREAM_BATCH_SIZE = 1000

#: The statements of a transaction's control.
CLICKHOUSE_BEGIN_TRANSACTION_SQL = "BEGIN TRANSACTION"
CLICKHOUSE_COMMIT_SQL = "COMMIT"
CLICKHOUSE_ROLLBACK_SQL = "ROLLBACK"
#: Why the statements of a transaction after a failed one are refused - the server rolled it back.
CLICKHOUSE_TRANSACTION_ABORTED_MESSAGE = (
    "A statement of the ClickHouse transaction failed - the server rolled the transaction back and takes no "
    "statement of it but its ROLLBACK"
)
#: What a transaction's client takes over from the client it is opened on.
CLICKHOUSE_TRANSACTION_CLIENT_ATTRIBUTES = (
    "host",
    "port",
    "user",
    "password",
    "database",
    "options",
    "cluster",
    "dialect",
    "query_class",
    "pool_statistics",
)
#: The column types of a table - of the database given, of the connection's with an empty name.
CLICKHOUSE_TABLE_COLUMN_TYPES_SQL = (
    "SELECT name, type FROM system.columns WHERE database = if($1 = '', currentDatabase(), $1) AND table = $2"
)

#: What a connection learns of the server: whether it has the table of its ClickHouse Keeper connections -
#: the keys of series (generateSerialID) need a Keeper it reaches.
CLICKHOUSE_SERVER_FACTS_SQL = (
    "SELECT (SELECT count() FROM system.tables WHERE database = 'system' AND name = 'zookeeper_connection') "
    "AS keeper_tables"
)
CLICKHOUSE_KEEPER_CONNECTIONS_SQL = "SELECT count() AS keepers FROM system.zookeeper_connection"
#: The next keys of a series.
CLICKHOUSE_KEY_SERIES_TAKE_SQL = "SELECT generateSerialID({series}) FROM numbers({count})"
#: How many keys a series lags behind the greatest key of its table - its next key taken, and the ones
#: up to the greatest key still to be.
CLICKHOUSE_KEY_SERIES_GAP_SQL = "SELECT (SELECT max({key}) FROM {table}) - generateSerialID({series}) AS gap"
#: Keys of a series taken and dropped - the series moved past them. Their greatest one is read: a key no
#: expression reads isn't taken.
CLICKHOUSE_KEY_SERIES_SKIP_SQL = "SELECT max(generateSerialID({series})) AS last FROM numbers({count})"
#: The format of an external table's data.
CLICKHOUSE_TAB_SEPARATED_FORMAT = "TabSeparated"
#: The name of the external table of the n-th set of a statement, and of its n-th column.
CLICKHOUSE_EXTERNAL_SET_NAME_TEMPLATE = "hare_set_{}"
CLICKHOUSE_EXTERNAL_SET_COLUMN_TEMPLATE = "value{}"
#: The characters of a text the ``TabSeparated`` format escapes, but the line break between rows.
CLICKHOUSE_TAB_SEPARATED_SPECIAL_CHARACTERS = ("\\", "\t", "\r", "\0")
#: How a text is escaped in a value of the ``TabSeparated`` format.
CLICKHOUSE_TAB_SEPARATED_ESCAPES = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r", "\0": "\\0"})

#: The refusal of a savepoint - a ClickHouse transaction has none.
CLICKHOUSE_NO_SAVEPOINTS_MESSAGE = "ClickHouse transactions have no savepoints"

#: The error of a statement whose connection was closed while it opened - closed in its turn.
CLICKHOUSE_CLOSED_WHILE_OPENING_MESSAGE = "The connection was closed while it was opening"

#: The error of a statement on a client closed and dropped - it opens no connection again.
CLICKHOUSE_DISCARDED_CLIENT_MESSAGE = (
    "The connection {alias!r} was closed and dropped - a query runs on the connection the context gives now"
)
