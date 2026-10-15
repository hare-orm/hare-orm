from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.types.sql_type_length import SqlTypeLength


class SqlType:
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, length: int) -> SqlTypeLength:
        return SqlTypeLength(self.name, length)

    def get_sql(self, sql_context: SqlContext) -> str:
        return f"{self.name}"
