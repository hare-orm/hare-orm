from __future__ import annotations

import datetime
import re

#: The DB_URL scheme and the connection config ``engine`` of the clickhouse-connect driver.
CLICKHOUSE_CONNECT_DRIVER_NAME = "clickhouse+clickhouse-connect"
#: The DB_URL scheme and the connection config ``engine`` of the clickhouse-driver driver.
CLICKHOUSE_DRIVER_DRIVER_NAME = "clickhouse+clickhouse-driver"

#: The moment the ticks of a column of moments count from.
CLICKHOUSE_MOMENT_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)

#: The settings every statement runs with:
#: - a LEFT JOIN's missing row reads as NULLs, not as its columns' default values;
#: - UPDATE and DELETE mutations finish before the statement returns;
#: - moments are read and written in UTC;
#: - a statement may be as long and as large as its values make it - every value is written into
#:   the text, and the server's defaults (256 KiB of text, 50 000 elements of its syntax tree) refuse
#:   a long ``__in`` list or a large JSON document an UPDATE writes;
#: - a JSON text is read by the parser taking an integer beyond 64 bits as a number - the default one
#:   reads no path of a document holding one (a JSON column writes 1e20 as 100000000000000000000).
CLICKHOUSE_SESSION_SETTINGS = {
    "join_use_nulls": 1,
    "mutations_sync": 2,
    "session_timezone": "UTC",
    "max_query_size": 1 << 30,
    "max_ast_elements": 100_000_000,
    "max_expanded_ast_elements": 100_000_000,
    "allow_simdjson": 0,
}

#: The settings a statement runs with on a server that has them:
#: - a lightweight DELETE finishes before the statement returns - a server without the setting
#:   (ClickHouse 24.3) waits for it as ``mutations_sync`` says;
#: - a text cast to a ``Dynamic`` or a ``Variant`` value stays a text - the server would read ``'5'``
#:   as a number;
#: - a subquery reads the columns of the query around it (experimental before ClickHouse 25.8).
CLICKHOUSE_OPTIONAL_SESSION_SETTINGS = {
    "lightweight_deletes_sync": 2,
    "cast_string_to_dynamic_use_inference": 0,
    "cast_string_to_variant_use_inference": 0,
    "allow_experimental_correlated_subqueries": 1,
}

#: The ClickHouse error codes of a missing database - the test database dropped twice is fine.
CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODES = frozenset({81})

#: The byte ClickHouse writes a type of no argument as, in a value of a ``Dynamic`` column's shared
#: variant (the type's binary encoding).
CLICKHOUSE_BINARY_PLAIN_TYPE_TAGS = {
    "Nothing": 0x00,
    "UInt8": 0x01,
    "UInt16": 0x02,
    "UInt32": 0x03,
    "UInt64": 0x04,
    "UInt128": 0x05,
    "UInt256": 0x06,
    "Int8": 0x07,
    "Int16": 0x08,
    "Int32": 0x09,
    "Int64": 0x0A,
    "Int128": 0x0B,
    "Int256": 0x0C,
    "Float32": 0x0D,
    "Float64": 0x0E,
    "Date": 0x0F,
    "Date32": 0x10,
    "String": 0x15,
    "UUID": 0x1D,
    "IPv4": 0x28,
    "IPv6": 0x29,
    "Bool": 0x2D,
}
#: The type named by each byte of a type of no argument.
CLICKHOUSE_BINARY_PLAIN_TYPES_BY_TAG = {tag: type_name for type_name, tag in CLICKHOUSE_BINARY_PLAIN_TYPE_TAGS.items()}
#: A named element of a tuple's type - its name, then its type.
CLICKHOUSE_BINARY_NAMED_ELEMENT_PATTERN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*) (.+)")
#: An enum's member - its quoted label and its number.
CLICKHOUSE_BINARY_ENUM_MEMBER_PATTERN = re.compile(r"'((?:[^'\\]|\\.)*)'\s*=\s*(-?\d+)")
#: A character of a quoted text escaped with a backslash.
CLICKHOUSE_BINARY_ESCAPED_CHARACTER_PATTERN = re.compile(r"\\(.)")
#: The most types whose parsed text and binary encoding are kept.
CLICKHOUSE_BINARY_TYPE_CACHE_MAX_SIZE = 1024


#: The bytes of the types taking arguments.
CLICKHOUSE_BINARY_DATETIME_TAG = 0x11
CLICKHOUSE_BINARY_ZONED_DATETIME_TAG = 0x12
CLICKHOUSE_BINARY_DATETIME64_TAG = 0x13
CLICKHOUSE_BINARY_ZONED_DATETIME64_TAG = 0x14
CLICKHOUSE_BINARY_FIXED_STRING_TAG = 0x16
CLICKHOUSE_BINARY_ENUM8_TAG = 0x17
CLICKHOUSE_BINARY_ENUM16_TAG = 0x18
#: ``Decimal32`` to ``Decimal256`` - the tag after the first is that of the next width.
CLICKHOUSE_BINARY_DECIMAL_TAGS = (0x19, 0x1A, 0x1B, 0x1C)
CLICKHOUSE_BINARY_ARRAY_TAG = 0x1E
CLICKHOUSE_BINARY_TUPLE_TAG = 0x1F
CLICKHOUSE_BINARY_NAMED_TUPLE_TAG = 0x20
CLICKHOUSE_BINARY_NULLABLE_TAG = 0x23
CLICKHOUSE_BINARY_LOW_CARDINALITY_TAG = 0x26
CLICKHOUSE_BINARY_MAP_TAG = 0x27
#: The byte of each type wrapping one other type, and the type each of the bytes names.
CLICKHOUSE_BINARY_WRAPPER_TAGS = {
    "Array": CLICKHOUSE_BINARY_ARRAY_TAG,
    "Nullable": CLICKHOUSE_BINARY_NULLABLE_TAG,
    "LowCardinality": CLICKHOUSE_BINARY_LOW_CARDINALITY_TAG,
}
CLICKHOUSE_BINARY_WRAPPERS_BY_TAG = {tag: type_name for type_name, tag in CLICKHOUSE_BINARY_WRAPPER_TAGS.items()}
#: The most digits of each decimal width, and its bytes.
CLICKHOUSE_BINARY_DECIMAL_WIDTHS = ((9, 4), (18, 8), (38, 16), (76, 32))
#: The bytes of each integer type, and whether it is signed.
CLICKHOUSE_BINARY_INTEGER_WIDTHS = {
    "UInt8": (1, False),
    "UInt16": (2, False),
    "UInt32": (4, False),
    "UInt64": (8, False),
    "UInt128": (16, False),
    "UInt256": (32, False),
    "Int8": (1, True),
    "Int16": (2, True),
    "Int32": (4, True),
    "Int64": (8, True),
    "Int128": (16, True),
    "Int256": (32, True),
}
#: The day a date counts from.
CLICKHOUSE_BINARY_EPOCH_DATE = datetime.date(1970, 1, 1)
#: The moment a moment counts from.
CLICKHOUSE_BINARY_EPOCH_MOMENT = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
#: The structure of a ``Dynamic`` column written with no type of its own - every value in the shared
#: variant: the structure's version (with the most types it keeps), no types, discriminators one byte
#: per row; the shared variant's discriminator, and NULL's.
CLICKHOUSE_DYNAMIC_STRUCTURE_VERSION = 1
CLICKHOUSE_DYNAMIC_WRITTEN_MAX_TYPES = 32
CLICKHOUSE_DYNAMIC_SHARED_DISCRIMINATOR = 0
CLICKHOUSE_VARIANT_NULL_DISCRIMINATOR = 0xFF
