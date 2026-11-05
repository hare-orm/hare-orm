from __future__ import annotations


class SqlTypes:
    """The SQL types the builder casts to by name."""

    FLOAT = "FLOAT"
    NUMERIC = "NUMERIC"
    INTEGER = "INTEGER"
    BIGINT = "BIGINT"

    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"
    TIMESTAMPTZ = "TIMESTAMPTZ"

    VARCHAR = "VARCHAR"
