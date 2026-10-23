from __future__ import annotations

import datetime
import decimal
import ipaddress
import re
import uuid

#: The start of a dictionary-encoded column type - ``LowCardinality(T)``.
CLICKHOUSE_LOW_CARDINALITY_PREFIX = "LowCardinality("
#: The numbers an ``Enum8`` and an ``Enum16`` hold.
CLICKHOUSE_ENUM8_RANGE = (-(2**7), 2**7 - 1)
CLICKHOUSE_ENUM16_RANGE = (-(2**15), 2**15 - 1)
#: How many numbers an ``Enum16`` holds - a ``CharEnumField``'s label is numbered within them.
CLICKHOUSE_ENUM_NUMBER_SPAN = 2**16
#: The ISO names hare's fields write their column types with -> ClickHouse's own name of the type,
#: the one the server reports (``VARCHAR(n)`` and ``CHAR(n)`` are ``String`` whatever their length).
CLICKHOUSE_ISO_TYPE_NAMES = {
    "TINYINT": "Int8",
    "SMALLINT": "Int16",
    "INT": "Int32",
    "INTEGER": "Int32",
    "BIGINT": "Int64",
    "REAL": "Float32",
    "FLOAT": "Float32",
    "DOUBLE": "Float64",
    "DOUBLE PRECISION": "Float64",
    "BOOL": "Bool",
    "BOOLEAN": "Bool",
    "TEXT": "String",
    "VARCHAR": "String",
    "CHAR": "String",
}
#: An ISO type name in a type's text, with the length a text type may give - outside quoted text.
CLICKHOUSE_ISO_TYPE_NAME_PATTERN = re.compile(
    r"'(?:[^'\\]|\\.)*'|\b(DOUBLE PRECISION|TINYINT|SMALLINT|INTEGER|INT|BIGINT|REAL|FLOAT|DOUBLE|BOOLEAN|BOOL|TEXT"
    r"|VARCHAR|CHAR)\b(?:\(\d+\))?"
)
#: The type of a ``Dynamic`` column.
CLICKHOUSE_DYNAMIC_TYPE = "Dynamic"
#: The wrapper of a type holding NULLs too.
CLICKHOUSE_NULLABLE_TYPE = "Nullable"
#: The most types a ``Dynamic`` column keeps in columns of their own - the rest share one.
CLICKHOUSE_DYNAMIC_MAX_TYPES = 254
#: The type a moment is kept as in a ``Dynamic`` column - the type of a ``DatetimeField``.
CLICKHOUSE_DYNAMIC_DATETIME_TYPE = "DateTime64(6, 'UTC')"
#: The type a ``Dynamic`` column keeps a value of each of these classes as, whatever the value.
CLICKHOUSE_DYNAMIC_PLAIN_TYPES: dict[type, str] = {
    bool: "Bool",
    float: "Float64",
    str: "String",
    datetime.date: "Date32",
    uuid.UUID: "UUID",
    ipaddress.IPv4Address: "IPv4",
    ipaddress.IPv6Address: "IPv6",
}
#: The integer types a ``Dynamic`` column keeps an int as - the first one holding it.
CLICKHOUSE_DYNAMIC_INTEGER_TYPES = (
    ("Int64", -(2**63), 2**63 - 1),
    ("UInt64", 0, 2**64 - 1),
    ("Int128", -(2**127), 2**127 - 1),
    ("UInt128", 0, 2**128 - 1),
    ("Int256", -(2**255), 2**255 - 1),
    ("UInt256", 0, 2**256 - 1),
)
#: The most digits a ``Decimal(P, S)`` holds.
CLICKHOUSE_DECIMAL_MAX_PRECISION = 76
#: The start of a ClickHouse type name -> the Python types the drivers read its values as (None: those
#: of the type inside); a longer start is matched first.
CLICKHOUSE_READ_PYTHON_TYPES: tuple[tuple[str, tuple[type, ...] | None], ...] = (
    ("LowCardinality(", None),
    ("Nullable(", None),
    ("DateTime", (datetime.datetime,)),
    ("Date", (datetime.date,)),
    ("Decimal", (decimal.Decimal,)),
    ("Bool", (bool,)),
    ("UInt", (int,)),
    ("Int", (int,)),
    ("Float", (float,)),
    ("String", (str,)),
    # clickhouse-connect reads the padded bytes, clickhouse-driver their text.
    ("FixedString", (str, bytes)),
    ("Enum", (str,)),
    ("UUID", (uuid.UUID,)),
    ("IPv4", (ipaddress.IPv4Address,)),
    ("IPv6", (ipaddress.IPv6Address,)),
    ("Array(", (list,)),
    ("Tuple(", (tuple,)),
    ("Map(", (dict,)),
)
#: The least integer a literal is read as a ``UInt64`` from - which no signed integer of an array or a map
#: shares a type with, so such a value of a signed field is written with its type.
CLICKHOUSE_UNSIGNED_LITERAL_FLOOR = 2**32
#: How the server names its unsigned integer types.
CLICKHOUSE_UNSIGNED_TYPE_PREFIX = "UInt"

#: The type of a JSONField's column on a server with the JSON type.
CLICKHOUSE_JSON_COLUMN_TYPE = "JSON"
#: The column of a JSON field keeping its text (``JSONField.keeps_json_text``) - and of every JSON
#: field on a driver reading no JSON column.
CLICKHOUSE_JSON_TEXT_COLUMN_TYPE = "String"

#: A string of a JSON text, and the ``:`` following it when it is a key (``colon``).
CLICKHOUSE_JSON_STRING_PATTERN = re.compile(r'"(?:[^"\\]|\\.)*"(?P<colon>\s*:)?')
#: A dot of a JSON key, and how a key of a ``JSON`` value holds it (json_type_escape_dots_in_keys) -
#: the server writes it back as a dot.
CLICKHOUSE_JSON_KEY_DOT = "."
CLICKHOUSE_ESCAPED_JSON_KEY_DOT = "%2E"
#: The integers a ``JSON`` column keeps as they are - an Int64 or a UInt64; another one is read back as
#: a float.
CLICKHOUSE_JSON_INTEGER_RANGE = (-(2**63), 2**64 - 1)
#: The largest integer a float holds exactly - an array of a ``JSON`` column holding a float reads
#: its integers as floats.
CLICKHOUSE_JSON_EXACT_FLOAT_INTEGER = 2**53
#: What a JSON text holds where its value may be one a ``JSON`` column doesn't keep - a null, an empty
#: object, an integer of 16 digits or more (past 2**53, its digits read as zeros); a text without any
#: is kept as it is.
CLICKHOUSE_JSON_NOT_KEPT_MARKS = ("null", "{}")
CLICKHOUSE_JSON_DIGITS_AS_ZEROS = str.maketrans("123456789", "000000000")
CLICKHOUSE_JSON_LONG_INTEGER_MARK = "0" * 16

#: A backslash escape of a ClickHouse string literal - the escaped character.
CLICKHOUSE_STRING_ESCAPE_PATTERN = re.compile(r"\\(.)", re.DOTALL)
#: How the server escapes a character of an enum label in the text of the type.
CLICKHOUSE_TYPE_TEXT_ESCAPES = {
    "\\": "\\\\",
    "'": "\\'",
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
    "\0": "\\0",
    "\b": "\\b",
    "\f": "\\f",
}
#: How many enum classes the labels the drivers read are kept for.
CLICKHOUSE_ENUM_DRIVER_LABELS_CACHE_SIZE = 256
#: The types ClickHouse keeps ``LowCardinality`` of - others it refuses (an enum, a decimal, a moment)
#: or allows only under ``allow_suspicious_low_cardinality_types`` (a number, a date, an address).
CLICKHOUSE_LOW_CARDINALITY_TYPE_NAMES = frozenset({"String", "FixedString", "UUID"})
