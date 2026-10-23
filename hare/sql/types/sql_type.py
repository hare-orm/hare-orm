from __future__ import annotations

from hare.sql.sql_context import SqlContext


class SqlType:
    """A SQL type named by itself in the SQL text (``VARCHAR``)."""

    def __init__(self, name: str) -> None:
        self.name = name

    def get_sql(self, sql_context: SqlContext) -> str:
        return f"{self.name}"
