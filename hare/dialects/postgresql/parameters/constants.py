from __future__ import annotations

import datetime
import decimal
import uuid

from hare.dialects.enums import ParameterPosition
from hare.sql.enums import JsonValueType

#: The most bind parameters one statement carries - the Bind message of the wire protocol counts
#: them in a signed 16-bit integer.
POSTGRESQL_MAX_BIND_PARAMETERS = 32767

#: A rough cutoff: `= ANY($1)` beat `IN (...)` by ~9% at 500 values and was no better at 3-10.
POSTGRES_IN_ARRAY_THRESHOLD = 20

#: The column types bulk_create(use_copy=True) takes - SQL_TYPE without its size - the same on both
#: drivers: COPY BINARY needs every type declared. Array, range and extension types aren't among
#: them.
COPY_SUPPORTED_SQL_TYPES = frozenset(
    {
        "SMALLINT",
        "INT",
        "INTEGER",
        "BIGINT",
        "REAL",
        "DOUBLE PRECISION",
        "NUMERIC",
        "DECIMAL",
        "BOOL",
        "BOOLEAN",
        "TEXT",
        "VARCHAR",
        "CHAR",
        "CHARACTER VARYING",
        "CHARACTER",
        "UUID",
        "DATE",
        "TIME",
        "TIMETZ",
        "TIMESTAMP",
        "TIMESTAMPTZ",
        "JSON",
        "JSONB",
        "BYTEA",
        "BLOB",
    }
)

#: The type a bare parameter of each Python type is cast to - bool before int, which it subclasses.
POSTGRESQL_PARAMETER_CASTS: tuple[tuple[type, str], ...] = (
    (bool, "boolean"),
    (datetime.datetime, "timestamptz"),
    (datetime.date, "date"),
    (datetime.time, "time"),
    (decimal.Decimal, "numeric"),
    (int, "bigint"),
    (float, "double precision"),
    (uuid.UUID, "uuid"),
)

#: The type a literal selected or compared on its own is cast to, before the number types - checked
#: in order: datetime subclasses date.
POSTGRESQL_SELECTED_LITERAL_TYPES: tuple[tuple[type, str], ...] = (
    (datetime.datetime, "TIMESTAMPTZ"),
    (datetime.date, "DATE"),
    (datetime.time, "TIMETZ"),
    (datetime.timedelta, "BIGINT"),
    (bytes, "BYTEA"),
)

#: The positions of a literal selected or compared on its own.
POSTGRESQL_SELECTED_LITERAL_POSITIONS = frozenset({ParameterPosition.SELECTED_VALUE, ParameterPosition.COMPARED_VALUE})

#: The type a literal written into a JSON object is cast to, where its value type alone decides it.
POSTGRESQL_JSON_OBJECT_VALUE_TYPES: dict[JsonValueType, str] = {
    JsonValueType.JSON: "JSONB",
    JsonValueType.BINARY: "BYTEA",
    JsonValueType.PLAIN: "TEXT",
}
