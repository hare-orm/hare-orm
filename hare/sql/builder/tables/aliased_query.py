from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.builder.tables.selectable import Selectable
from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table


class AliasedQuery(Selectable):
    """A source of rows read by its name - a CTE's or a derived table's."""

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

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Retarget the body while keeping the alias used by the consuming query."""
        if self.query is not None:
            self.query = self.query.replace_table(current_table, new_table)
        return self

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, AliasedQuery) and self.name == other.name

    def __hash__(self) -> int:
        return hash(str(self.name))
