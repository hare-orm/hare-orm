from hare.sql.sql_type import SqlType


class SqlTypes:
    BOOLEAN = "BOOLEAN"
    INTEGER = "INTEGER"
    FLOAT = "FLOAT"
    REAL = "REAL"
    NUMERIC = "NUMERIC"
    BIGINT = "BIGINT"

    DATE = "DATE"
    TIME = "TIME"
    TIMESTAMP = "TIMESTAMP"
    TIMESTAMPTZ = "TIMESTAMPTZ"
    TIMETZ = "TIMETZ"
    BYTEA = "BYTEA"

    CHAR = SqlType("CHAR")
    VARCHAR = SqlType("VARCHAR")
    BINARY = SqlType("BINARY")
