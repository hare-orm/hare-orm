from __future__ import annotations

import datetime

from hare.dialects.clickhouse.enums import ClickhouseTypeName

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
#:   reads no path of a document holding one (a JSON column writes 1e20 as 100000000000000000000);
#: - a JSON boolean stays a boolean - an array holding numbers and booleans would hold true as 1, a
#:   document a filter compares with would hold true beside a text as the text 'true';
#: - a JSON string looking like a date or a moment stays a string - a JSON column would hold it as a
#:   date, read back as one, its offset lost;
#: - a lightweight DELETE finishes before the statement returns;
#: - a text cast to a ``Dynamic`` or a ``Variant`` value stays a text - the server would read ``'5'``
#:   as a number;
#: - a dot in a key of a JSON value is part of the key, not a nested path;
#: - a ``Dynamic`` value - a JSON path's - orders rows, and groups them where a grouping takes the ordering;
#: - an error's text is the server's message without the server's own stack trace, which the native
#:   protocol sends with it;
#: - a DISTINCT over ordered rows reads them as any rows - the optimization of ordered input kept
#:   duplicates of a UNION, INTERSECT or EXCEPT of ordered branches, also inside a subquery, whose own
#:   SETTINGS the set operation doesn't read.
CLICKHOUSE_SESSION_SETTINGS = {
    "join_use_nulls": 1,
    "mutations_sync": 2,
    "session_timezone": "UTC",
    "max_query_size": 1 << 30,
    "max_ast_elements": 100_000_000,
    "max_expanded_ast_elements": 100_000_000,
    "allow_simdjson": 0,
    "input_format_json_read_bools_as_numbers": 0,
    "input_format_json_read_bools_as_strings": 0,
    "input_format_try_infer_dates": 0,
    "input_format_try_infer_datetimes": 0,
    "lightweight_deletes_sync": 2,
    "cast_string_to_dynamic_use_inference": 0,
    "cast_string_to_variant_use_inference": 0,
    "json_type_escape_dots_in_keys": 1,
    "allow_suspicious_types_in_order_by": 1,
    "allow_suspicious_types_in_group_by": 1,
    "calculate_text_stack_trace": 0,
    "optimize_distinct_in_order": 0,
}


#: The ClickHouse error codes of a missing database - the test database dropped twice is fine.
CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODES = frozenset({81})

#: The byte ClickHouse writes a type of no argument as, in a value of a ``Dynamic`` column's shared
#: variant (the type's binary encoding).
CLICKHOUSE_BINARY_PLAIN_TYPE_TAGS: dict[str, int] = {
    ClickhouseTypeName.NOTHING: 0x00,
    ClickhouseTypeName.UINT8: 0x01,
    ClickhouseTypeName.UINT16: 0x02,
    ClickhouseTypeName.UINT32: 0x03,
    ClickhouseTypeName.UINT64: 0x04,
    ClickhouseTypeName.UINT128: 0x05,
    ClickhouseTypeName.UINT256: 0x06,
    ClickhouseTypeName.INT8: 0x07,
    ClickhouseTypeName.INT16: 0x08,
    ClickhouseTypeName.INT32: 0x09,
    ClickhouseTypeName.INT64: 0x0A,
    ClickhouseTypeName.INT128: 0x0B,
    ClickhouseTypeName.INT256: 0x0C,
    ClickhouseTypeName.FLOAT32: 0x0D,
    ClickhouseTypeName.FLOAT64: 0x0E,
    ClickhouseTypeName.DATE: 0x0F,
    ClickhouseTypeName.DATE32: 0x10,
    ClickhouseTypeName.STRING: 0x15,
    ClickhouseTypeName.UUID: 0x1D,
    ClickhouseTypeName.IPV4: 0x28,
    ClickhouseTypeName.IPV6: 0x29,
    ClickhouseTypeName.BOOL: 0x2D,
}
#: The type named by each byte of a type of no argument.
CLICKHOUSE_BINARY_PLAIN_TYPES_BY_TAG = {tag: type_name for type_name, tag in CLICKHOUSE_BINARY_PLAIN_TYPE_TAGS.items()}
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
CLICKHOUSE_BINARY_WRAPPER_TAGS: dict[str, int] = {
    ClickhouseTypeName.ARRAY: CLICKHOUSE_BINARY_ARRAY_TAG,
    ClickhouseTypeName.NULLABLE: CLICKHOUSE_BINARY_NULLABLE_TAG,
    ClickhouseTypeName.LOW_CARDINALITY: CLICKHOUSE_BINARY_LOW_CARDINALITY_TAG,
}
CLICKHOUSE_BINARY_WRAPPERS_BY_TAG = {tag: type_name for type_name, tag in CLICKHOUSE_BINARY_WRAPPER_TAGS.items()}
#: The most digits of each decimal width, and its bytes.
CLICKHOUSE_BINARY_DECIMAL_WIDTHS = ((9, 4), (18, 8), (38, 16), (76, 32))
#: The bytes of each integer type, and whether it is signed.
CLICKHOUSE_BINARY_INTEGER_WIDTHS: dict[str, tuple[int, bool]] = {
    ClickhouseTypeName.UINT8: (1, False),
    ClickhouseTypeName.UINT16: (2, False),
    ClickhouseTypeName.UINT32: (4, False),
    ClickhouseTypeName.UINT64: (8, False),
    ClickhouseTypeName.UINT128: (16, False),
    ClickhouseTypeName.UINT256: (32, False),
    ClickhouseTypeName.INT8: (1, True),
    ClickhouseTypeName.INT16: (2, True),
    ClickhouseTypeName.INT32: (4, True),
    ClickhouseTypeName.INT64: (8, True),
    ClickhouseTypeName.INT128: (16, True),
    ClickhouseTypeName.INT256: (32, True),
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
