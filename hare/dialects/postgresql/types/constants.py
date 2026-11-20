from __future__ import annotations

import re

from hare.lazy_loading.lazy_pattern import LazyPattern

POSTGRESQL_PHYSICAL_TYPE_CACHE_SIZE = 1024
POSTGRESQL_ARRAY_TYPE_SUFFIX = re.compile(r"(?:\s*\[\s*\])+$")

#: The column types PostgreSQL has no MIN/MAX aggregate of - by their name before any parameters.
POSTGRESQL_TYPES_WITHOUT_MIN_MAX = frozenset(
    {
        "bool",
        "boolean",
        "uuid",
        "json",
        "jsonb",
        "macaddr",
        "macaddr8",
        "hstore",
        "ltree",
        "tsvector",
        "geometry",
        "geography",
        "vector",
        "int4range",
        "int8range",
        "numrange",
        "daterange",
        "tsrange",
        "tstzrange",
        "int4multirange",
        "int8multirange",
        "nummultirange",
        "datemultirange",
        "tsmultirange",
        "tstzmultirange",
    }
)


#: Declared aliases mapped to the built-in name format_type() reports.
POSTGRESQL_CANONICAL_TYPE_NAMES: dict[str, str] = {
    "int": "integer",
    "int4": "integer",
    "integer": "integer",
    "serial": "integer",
    "serial4": "integer",
    "int2": "smallint",
    "smallint": "smallint",
    "smallserial": "smallint",
    "serial2": "smallint",
    "int8": "bigint",
    "bigint": "bigint",
    "bigserial": "bigint",
    "serial8": "bigint",
    "bool": "boolean",
    "boolean": "boolean",
    "varchar": "character varying",
    "character varying": "character varying",
    "char": "character",
    "character": "character",
    "bpchar": "character",
    "text": "text",
    "decimal": "numeric",
    "numeric": "numeric",
    "float": "double precision",
    "float8": "double precision",
    "double precision": "double precision",
    "float4": "real",
    "real": "real",
    "timestamptz": "timestamp with time zone",
    "timestamp with time zone": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
    "timestamp without time zone": "timestamp without time zone",
    "timetz": "time with time zone",
    "time with time zone": "time with time zone",
    "time": "time without time zone",
    "time without time zone": "time without time zone",
    "date": "date",
    "uuid": "uuid",
    "json": "json",
    "jsonb": "jsonb",
    "bytea": "bytea",
}

#: A Postgres type name in its own parameter-free spelling - ``timestamp(3) with time zone`` keeps
#: its precision apart from the time zone suffix.
POSTGRESQL_TYPE_RE = LazyPattern(
    r"^(?P<name>[a-z_][a-z0-9_ ]*?)\s*(?:\((?P<parameters>[^)]*)\))?"
    r"(?P<time_zone>\s+with(?:out)?\s+time\s+zone)?$",
    re.IGNORECASE,
)

#: The length Postgres gives a ``character`` column declared without one.
POSTGRESQL_DEFAULT_CHARACTER_LENGTH = "1"
