from __future__ import annotations

from hare.sql.sql_context import SqlContext


class SqlTypeLength:
    def __init__(self, name: str, length: int) -> None:
        self.name = name
        self.length = length

    def get_sql(self, sql_context: SqlContext) -> str:
        return f"{self.name}({self.length})"
