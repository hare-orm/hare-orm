from __future__ import annotations

from enum import StrEnum


class ClickhouseDialectName(StrEnum):
    """The name ClickHouse's dialect is registered under."""

    CLICKHOUSE = "clickhouse"


class ClickhouseClause(StrEnum):
    """A clause ClickHouse's QuerySet methods set on a query (``QueryBuilder._dialect_clauses``)."""

    #: ``FINAL`` after the model's table - its rows merged as the engine merges them.
    FINAL = "final"
    #: ``SAMPLE <rows>`` - about that many rows.
    SAMPLE_ROWS = "sample_rows"
    #: ``OFFSET <fraction>`` of a ``SAMPLE`` - the part of the sampled key range read.
    SAMPLE_OFFSET = "sample_offset"
    #: ``PREWHERE <condition>`` - read before the other columns.
    PREWHERE = "prewhere"
    #: ``LIMIT <n> [OFFSET <m>] BY <expressions>`` - rows per group of values.
    LIMIT_BY = "limit_by"
    #: ``SETTINGS <name> = <value>, ...`` of the statement.
    SETTINGS = "settings"
    #: ``WITH TOTALS`` after ``GROUP BY`` - a row of the aggregates over every group.
    WITH_TOTALS = "with_totals"


class ClickhouseLookup(StrEnum):
    """A lookup ClickHouse adds to every value."""

    #: ``GLOBAL IN`` - the list or subquery read once, on the server the query was sent to, and sent to
    #: every shard of a ``Distributed`` table.
    GLOBAL_IN = "global_in"
    #: ``GLOBAL NOT IN``.
    GLOBAL_NOT_IN = "global_not_in"


class ClickhouseTypeName(StrEnum):
    """A ClickHouse type's own name - the name the server reports it by, without its arguments."""

    INT8 = "Int8"
    INT16 = "Int16"
    INT32 = "Int32"
    INT64 = "Int64"
    INT128 = "Int128"
    INT256 = "Int256"
    UINT8 = "UInt8"
    UINT16 = "UInt16"
    UINT32 = "UInt32"
    UINT64 = "UInt64"
    UINT128 = "UInt128"
    UINT256 = "UInt256"
    FLOAT32 = "Float32"
    FLOAT64 = "Float64"
    BFLOAT16 = "BFloat16"
    DECIMAL = "Decimal"
    DECIMAL32 = "Decimal32"
    DECIMAL64 = "Decimal64"
    DECIMAL128 = "Decimal128"
    DECIMAL256 = "Decimal256"
    BOOL = "Bool"
    STRING = "String"
    FIXED_STRING = "FixedString"
    UUID = "UUID"
    IPV4 = "IPv4"
    IPV6 = "IPv6"
    DATE = "Date"
    DATE32 = "Date32"
    DATETIME = "DateTime"
    DATETIME64 = "DateTime64"
    ENUM = "Enum"
    ENUM8 = "Enum8"
    ENUM16 = "Enum16"
    ARRAY = "Array"
    TUPLE = "Tuple"
    MAP = "Map"
    NESTED = "Nested"
    NULLABLE = "Nullable"
    LOW_CARDINALITY = "LowCardinality"
    NOTHING = "Nothing"
    VARIANT = "Variant"
    DYNAMIC = "Dynamic"
    JSON = "JSON"
    POINT = "Point"
    RING = "Ring"
    POLYGON = "Polygon"
    MULTI_POLYGON = "MultiPolygon"
    LINE_STRING = "LineString"
    MULTI_LINE_STRING = "MultiLineString"
