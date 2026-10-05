from __future__ import annotations

import datetime
import decimal
import uuid

#: Array element SQL type a large ``__in``/``__not_in`` list with no known field type is bound as,
#: by the Python type of its values - looked up along each value's MRO, so an ``IntEnum`` member
#: binds as a plain integer. ``datetime`` is decided separately (aware vs naive), since it is also a
#: ``date``.
POSTGRES_ARRAY_ELEMENT_TYPE_BY_PYTHON_TYPE: dict[type, str] = {
    bool: "BOOLEAN",
    int: "BIGINT",
    float: "DOUBLE PRECISION",
    decimal.Decimal: "NUMERIC",
    str: "TEXT",
    uuid.UUID: "UUID",
    bytes: "BYTEA",
    datetime.date: "DATE",
    datetime.time: "TIME",
    datetime.timedelta: "INTERVAL",
}

#: Array element SQL type for an integer outside BIGINT's range.
POSTGRES_WIDE_INTEGER_ARRAY_ELEMENT_TYPE = "NUMERIC"

#: Array element SQL types for timezone-aware and naive datetimes.
POSTGRES_AWARE_DATETIME_ARRAY_ELEMENT_TYPE = "TIMESTAMPTZ"

POSTGRES_NAIVE_DATETIME_ARRAY_ELEMENT_TYPE = "TIMESTAMP"

#: BIGINT's value range.
POSTGRES_BIGINT_MIN = -(2**63)

POSTGRES_BIGINT_MAX = 2**63 - 1

#: A path segment reading a text value without its accents - PostgreSQL's ``unaccent()``.
UNACCENT_PATH_SEGMENT = "unaccent"

#: The extension the trigram lookups and functions come from.
TRIGRAM_EXTENSION = "pg_trgm"

#: The extension ``unaccent()`` comes from.
UNACCENT_EXTENSION = "unaccent"
