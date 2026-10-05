from __future__ import annotations

import re
from typing import Any

from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.base.connection.connection_options import ConnectionOptions
from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
from hare.dialects.enums import ConnectionOptionType

#: The port of ClickHouse's HTTP interface.
CLICKHOUSE_DEFAULT_HTTP_PORT = 8123
#: The port of ClickHouse's native protocol.
CLICKHOUSE_DEFAULT_NATIVE_PORT = 9000
#: The fractional digits of a stored moment - microseconds, as Python's ``datetime``.
CLICKHOUSE_DATETIME_PRECISION = 6
#: The most digits a ``Decimal128`` holds - a wider decimal literal is a ``Decimal256``.
CLICKHOUSE_DECIMAL128_MAX_DIGITS = 38
#: The integers a number literal is read as exactly - ``Int64`` and ``UInt64``; ClickHouse reads a
#: wider one as a ``Float64``, losing its low digits.
CLICKHOUSE_INTEGER_LITERAL_RANGE = (-(2**63), 2**64 - 1)
#: The integer types wider than 64 bits and the numbers each holds - a literal beyond
#: ``CLICKHOUSE_INTEGER_LITERAL_RANGE`` is read from its text as the first one holding it.
CLICKHOUSE_WIDE_INTEGER_TYPES = (
    ("Int128", -(2**127), 2**127 - 1),
    ("UInt128", 0, 2**128 - 1),
    ("Int256", -(2**255), 2**255 - 1),
    ("UInt256", 0, 2**256 - 1),
)
#: The first and the last year a ``Date32`` column and a ``DateTime64`` column store, whole - the
#: server turns a value outside them into the nearest one without an error, so hare refuses it.
CLICKHOUSE_FIRST_YEAR = 1900
CLICKHOUSE_LAST_YEAR = 2299
#: The error of a value outside that range.
CLICKHOUSE_TEMPORAL_RANGE_MESSAGE = (
    "{value} is outside the dates ClickHouse stores, 1900-01-01 to 2299-12-31 - the server would "
    "write the nearest of them instead"
)
#: The column type of a ``DatetimeField``.
CLICKHOUSE_DATETIME_COLUMN_TYPE = f"DateTime64({CLICKHOUSE_DATETIME_PRECISION}, 'UTC')"
#: The escapes of a string literal - a backslash is an escape character in ClickHouse's strings.
CLICKHOUSE_STRING_ESCAPES = str.maketrans({"\\": "\\\\", "'": "\\'"})
#: A quoted string, name or alias of the SQL text - a backslash escapes the next character, a
#: doubled quote stands for itself, an unclosed one runs to the end.
CLICKHOUSE_QUOTED_SQL_PATTERN = (
    r"""'(?:\\(?:.|\Z)|''|[^'\\])*(?:'|\Z)"""
    r"""|"(?:\\(?:.|\Z)|""|[^"\\])*(?:"|\Z)"""
    r"""|`(?:\\(?:.|\Z)|``|[^`\\])*(?:`|\Z)"""
)
#: A numbered parameter placeholder outside quotes - ``$`` and the parameter's number, captured -
#: or a quoted part of the SQL text, matched to be kept as it is.
CLICKHOUSE_PLACEHOLDER_PATTERN = re.compile(CLICKHOUSE_QUOTED_SQL_PATTERN + r"|\$(\d+)", re.DOTALL)
#: A statement-ending semicolon outside quotes, or a quoted part of the SQL text.
CLICKHOUSE_STATEMENT_END_PATTERN = re.compile(CLICKHOUSE_QUOTED_SQL_PATTERN + r"|;", re.DOTALL)
#: ClickHouse's column types by their name without arguments, lowercase -> hare's field class.
CLICKHOUSE_TYPE_MAP: list[tuple[str, str, dict[str, Any]]] = [
    ("datetime", "hare.fields.data.temporal.DatetimeField", {}),
    ("datetime64", "hare.fields.data.temporal.DatetimeField", {}),
    ("date", "hare.fields.data.temporal.DateField", {}),
    ("date32", "hare.fields.data.temporal.DateField", {}),
    ("uuid", "hare.fields.data.uuid_field.UUIDField", {}),
    ("decimal", "hare.fields.data.numeric.DecimalField", {"max_digits": 20, "decimal_places": 6}),
    ("int64", "hare.fields.data.numeric.BigIntField", {}),
    ("int32", "hare.fields.data.numeric.IntField", {}),
    ("int16", "hare.fields.data.numeric.SmallIntField", {}),
    ("int8", "hare.fields.data.numeric.SmallIntField", {}),
    ("uint8", "hare.dialects.clickhouse.fields.UInt8Field", {}),
    ("uint16", "hare.dialects.clickhouse.fields.UInt16Field", {}),
    ("uint32", "hare.dialects.clickhouse.fields.UInt32Field", {}),
    ("uint64", "hare.dialects.clickhouse.fields.UInt64Field", {}),
    ("uint128", "hare.dialects.clickhouse.fields.UInt128Field", {}),
    ("uint256", "hare.dialects.clickhouse.fields.UInt256Field", {}),
    ("int128", "hare.dialects.clickhouse.fields.Int128Field", {}),
    ("int256", "hare.dialects.clickhouse.fields.Int256Field", {}),
    ("bool", "hare.fields.data.boolean_field.BooleanField", {}),
    ("float32", "hare.dialects.clickhouse.fields.Float32Field", {}),
    ("float64", "hare.fields.data.numeric.FloatField", {}),
    ("string", "hare.fields.data.text.TextField", {}),
    ("fixedstring", "hare.dialects.clickhouse.fields.FixedStringField", {}),
    ("ipv4", "hare.fields.data.network.IPv4AddressField", {}),
    ("ipv6", "hare.fields.data.network.IPAddressField", {}),
    ("point", "hare.gis.fields.PointField", {}),
    ("linestring", "hare.gis.fields.LineStringField", {}),
    ("multilinestring", "hare.gis.fields.MultiLineStringField", {}),
    ("polygon", "hare.gis.fields.PolygonField", {}),
    ("multipolygon", "hare.gis.fields.MultiPolygonField", {}),
]
#: The wrappers of a column type that change no field - a nullable column and a dictionary-encoded one.
CLICKHOUSE_TYPE_WRAPPERS = ("Nullable(", "LowCardinality(")
#: The column types that are never ``Nullable`` - a container's NULL is written as an empty one (a
#: geometry's too, but a point's, which has none), a ``Variant`` and a ``Dynamic`` column hold NULL
#: themselves.
CLICKHOUSE_NEVER_NULLABLE_TYPE_PREFIXES = (
    "Array(",
    "Map(",
    "Tuple(",
    "Nested(",
    "Variant(",
    "Dynamic",
    "Point",
    "Ring",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
)
#: The name of the connection's database - the namespace of its tables.
CLICKHOUSE_CURRENT_DATABASE_SQL = "SELECT currentDatabase() AS name"
#: The tables of a database.
CLICKHOUSE_TABLE_NAMES_SQL = (
    "SELECT name FROM system.tables WHERE database = $1 AND NOT is_temporary AND engine NOT LIKE '%View' "
    # The storage of a materialized view's own is a table of the view.
    "AND name NOT LIKE '.inner%' ORDER BY name"
)
#: Whether a table exists in the connection's database.
CLICKHOUSE_TABLE_EXISTS_SQL = "SELECT 1 AS found FROM system.tables WHERE database = currentDatabase() AND name = $1"
#: A table's engine, sort, primary key and comment.
CLICKHOUSE_TABLE_SQL = (
    "SELECT engine_full, sorting_key, primary_key, partition_key, sampling_key, create_table_query, comment "
    "FROM system.tables WHERE database = $1 AND name = $2"
)
#: A table's columns, in order.
CLICKHOUSE_COLUMNS_SQL = (
    "SELECT name, type, default_kind AS default_type, default_expression, comment, is_in_primary_key, "
    "compression_codec "
    "FROM system.columns "
    "WHERE database = $1 AND table = $2 ORDER BY position"
)
#: How ``system.columns`` names a column the database computes - on write, and on read.
CLICKHOUSE_GENERATED_COLUMN_TYPES = frozenset({"MATERIALIZED", "ALIAS"})
CLICKHOUSE_VIRTUAL_COLUMN_TYPE = "ALIAS"
#: The type of the data skipping index hare writes for an index naming no type of its own.
CLICKHOUSE_DEFAULT_INDEX_TYPE = "minmax"
CLICKHOUSE_DEFAULT_INDEX_GRANULARITY_SQL = " GRANULARITY 1"
#: The arguments of each data skipping index type hare's index classes declare, in the type's order.
CLICKHOUSE_INDEX_TYPE_ARGUMENTS = {
    "set": ("max_rows",),
    "bloom_filter": ("false_positive",),
    "ngrambf_v1": ("ngram_size", "filter_size", "hash_functions", "seed"),
    "tokenbf_v1": ("filter_size", "hash_functions", "seed"),
}
#: A table's data skipping indexes.
CLICKHOUSE_INDEXES_SQL = (
    "SELECT name, expr, type, type_full, granularity FROM system.data_skipping_indices "
    "WHERE database = $1 AND table = $2 ORDER BY name"
)
#: The settings of a ClickHouse connection beyond its address and credentials.
CLICKHOUSE_CONNECTION_OPTIONS = ConnectionOptions(
    ConnectionOption("secure", ConnectionOptionType.BOOLEAN),
    ConnectionOption("native_port", ConnectionOptionType.WHOLE_NUMBER, minimum=1, maximum=65535),
    ConnectionOption("compress", ConnectionOptionType.BOOLEAN),
    ConnectionOption("connect_timeout", ConnectionOptionType.SECONDS, positive=True, maximum=3600),
    ConnectionOption("send_receive_timeout", ConnectionOptionType.SECONDS, positive=True, maximum=86400),
    ConnectionOption("cluster", ConnectionOptionType.TEXT),
    ConnectionOption("async_insert", ConnectionOptionType.BOOLEAN),
    ConnectionOption("wait_for_async_insert", ConnectionOptionType.BOOLEAN),
    ConnectionOption("transactions", ConnectionOptionType.BOOLEAN),
    ConnectionOption("keeper_hosts", ConnectionOptionType.TEXT),
)
#: The beginning of the count following an UPDATE or DELETE - the rows the write matches, the
#: table and its condition after it.
CLICKHOUSE_ROW_COUNT_SQL = "SELECT count() AS hare_row_count FROM "

CLICKHOUSE_DIALECT = ClickhouseDialect()
#: The dialect of a connection to a server with the JSON type - a JSONField is a JSON column there.
CLICKHOUSE_NATIVE_JSON_DIALECT = ClickhouseDialect(stores_json_natively=True)
#: The functions reading a value of a dictionary by its key - with the attribute's own default for a
#: key the dictionary doesn't hold, and with a given one.
CLICKHOUSE_DICTIONARY_FUNCTION = "dictGet"
CLICKHOUSE_DICTIONARY_DEFAULT_FUNCTION = "dictGetOrDefault"
#: The function making the key of a dictionary keyed by several columns.
CLICKHOUSE_TUPLE_FUNCTION = "tuple"
