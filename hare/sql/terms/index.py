from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term


class Index(Term):
    def __init__(self, name: str, alias: str | None = None) -> None:
        super().__init__(alias)
        self.name = name

    def get_sql(self, sql_context: SqlContext) -> str:
        return sql_context.quote(self.name)
