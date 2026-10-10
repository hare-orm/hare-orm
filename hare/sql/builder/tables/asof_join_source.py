from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.sql.builder.tables.aliased_query import AliasedQuery
from hare.sql.enums import JoinType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.builder.tables.selectable import Selectable
    from hare.sql.sql_context import SqlContext


class AsofJoinSource(AliasedQuery):
    """A table or subquery joined ``ASOF LEFT`` under ``name`` - ``ASOF LEFT JOIN "quote" "name" ON
    ...``; equal to another of the same name, so it is joined once.

    Args:
        name: The name it is joined under.
        query: The table or subquery.
    """

    join_type: ClassVar[JoinType | None] = JoinType.ASOF_LEFT  # type: ignore[misc]

    def __init__(self, name: str, query: Selectable) -> None:
        super().__init__(name, query)

    def get_sql(self, sql_context: SqlContext) -> str:
        source_sql = self.query.get_sql(sql_context.copy(subquery=True, with_alias=False))  # type: ignore[union-attr]
        return sql_context.format_alias_sql(source_sql, self.alias)
