from __future__ import annotations

import datetime
import ipaddress
import re
import uuid
from decimal import Decimal

#: The ClickHouse error codes of a statement against the data - a violated CHECK constraint.
CLICKHOUSE_INTEGRITY_ERROR_CODES = frozenset({469})

#: The ClickHouse error codes of a connection that can't be used: authentication failed, the server
#: refused or dropped the connection.
CLICKHOUSE_CONNECTION_ERROR_CODES = frozenset({194, 209, 210, 516})

#: Where a ClickHouse error message names its error code - ``Code: 469.``.
CLICKHOUSE_ERROR_CODE_PREFIX = "Code: "

#: The arguments a client takes besides its connection settings - every other one must be a setting.
CLICKHOUSE_CLIENT_BASE_SETTINGS = frozenset({"connection_alias", "fetch_inserted"})

#: The column types a server may lack - the line geometries came after ClickHouse 24.3, ``Variant`` and
#: ``Dynamic`` are experimental before 25.3 (``Features.supports_variant_types``).
CLICKHOUSE_OPTIONAL_DATA_TYPES = frozenset({"LineString", "MultiLineString", "Variant", "Dynamic"})
CLICKHOUSE_VARIANT_DATA_TYPES = frozenset({"Variant", "Dynamic"})
#: The names of the column types a server may lack, in a column type.
CLICKHOUSE_OPTIONAL_DATA_TYPE_PATTERN = re.compile(r"\b(LineString|MultiLineString|Variant|Dynamic)\b")
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
#: Why a transaction whose ClickHouse Keeper session ended goes no further.
CLICKHOUSE_ROW_LOCKS_LOST_MESSAGE = (
    "The ClickHouse Keeper session holding the row locks of the transaction was lost - another transaction may "
    "have locked the rows since"
)
#: The server's code of a statement it doesn't run - in a transaction, one of a table or a type of query
#: transactions don't take.
CLICKHOUSE_NOT_IMPLEMENTED_ERROR_CODE = 48
#: What a transaction's client takes over from the client it is opened on.
CLICKHOUSE_TRANSACTION_CLIENT_ATTRIBUTES = (
    "host",
    "port",
    "user",
    "password",
    "database",
    "options",
    "cluster",
    "missing_data_types",
    "dialect",
    "query_class",
    "pool_statistics",
)
#: The column types of a table of the connection's database.
CLICKHOUSE_TABLE_COLUMN_TYPES_SQL = (
    "SELECT name, type FROM system.columns WHERE database = currentDatabase() AND table = $1"
)

#: What a connection learns of the server: whether it has the table of its ClickHouse Keeper connections -
#: the keys of series (generateSerialID) need a Keeper it reaches - and which of the column types a
#: server may lack it has.
CLICKHOUSE_SERVER_FACTS_SQL = (
    "SELECT (SELECT count() FROM system.tables WHERE database = 'system' AND name = 'zookeeper_connection') "
    "AS keeper_tables, (SELECT groupArray(name) FROM system.data_type_families WHERE name IN "
    "('LineString', 'MultiLineString', 'Variant', 'Dynamic')) AS present_data_types"
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
