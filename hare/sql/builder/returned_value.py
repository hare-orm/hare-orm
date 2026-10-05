from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReturnedValue:
    """One value a ``RETURNING`` clause returns.

    Attributes:
        sql: The expression's SQL.
        alias_sql: The quoted name the value is returned under, None to keep the expression's own.
        column_name: The name of a plain column the value is read back by - a dialect that names a
            returned column otherwise (SQLite before 3.36 keeps a quoted name's quotes) returns it
            under this name.
    """

    sql: str
    alias_sql: str | None = None
    column_name: str | None = None
