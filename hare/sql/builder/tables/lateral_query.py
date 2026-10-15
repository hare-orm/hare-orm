from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.builder.tables.aliased_query import AliasedQuery

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.builder.tables.selectable import Selectable
    from hare.sql.sql_context import SqlContext


class LateralQuery(AliasedQuery):
    """A subquery joined in ``FROM`` under ``name`` that reads the columns of the tables before it -
    ``LATERAL (SELECT ...) "name"``; equal to another of the same name, so it is joined once.

    Args:
        name: The name it is joined under.
        query: The subquery.
    """

    def __init__(self, name: str, query: Selectable) -> None:
        super().__init__(name, query)

    def get_sql(self, sql_context: SqlContext) -> str:
        subquery_sql = self.query.get_sql(sql_context.copy(subquery=True, with_alias=False))  # type: ignore[union-attr]
        return sql_context.format_alias_sql(sql_context.dialect.clauses.get_lateral_sql(subquery_sql), self.alias)
