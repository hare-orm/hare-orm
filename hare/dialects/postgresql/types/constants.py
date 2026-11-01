from __future__ import annotations

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
