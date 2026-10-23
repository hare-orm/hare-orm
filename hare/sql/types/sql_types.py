from __future__ import annotations

from hare.sql.types.sql_type import SqlType


class SqlTypes:
    """The SQL types the builder casts to by name."""

    FLOAT = "FLOAT"
    NUMERIC = "NUMERIC"
    BIGINT = "BIGINT"

    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"
    TIMESTAMPTZ = "TIMESTAMPTZ"

    VARCHAR = SqlType("VARCHAR")
