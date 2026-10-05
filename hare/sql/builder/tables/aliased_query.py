from __future__ import annotations

from typing import Any

from hare.sql.builder.tables.selectable import Selectable
from hare.sql.sql_context import SqlContext


class AliasedQuery(Selectable):
    #: Unlike the base Selectable, name is required here, so alias is never None.
    alias: str

    def __init__(
        self,
        name: str,
        query: Selectable | None = None,
    ) -> None:
        super().__init__(alias=name)
        self.name = name
        self.query = query

    def get_sql(self, sql_context: SqlContext) -> str:
        if self.query is None:
            return sql_context.quote(self.name)
        return self.query.get_sql(sql_context)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, AliasedQuery) and self.name == other.name

    def __hash__(self) -> int:
        return hash(str(self.name))
