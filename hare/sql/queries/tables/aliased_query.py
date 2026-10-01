from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext

if TYPE_CHECKING:
    pass
from hare.sql.queries.tables.selectable import Selectable


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

    def get_sql(self, ctx: SqlContext) -> str:
        if self.query is None:
            return ctx.quote(self.name)
        return self.query.get_sql(ctx)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, AliasedQuery) and self.name == other.name

    def __hash__(self) -> int:
        return hash(str(self.name))
