from __future__ import annotations

from collections.abc import Iterator

from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term


class IsDistinctFromCriterion(Criterion):
    """``<left> IS DISTINCT FROM <right>``, or the dialect's own form of it - a NULL-safe inequality: TRUE when the
    two differ, one NULL included, FALSE when both are NULL; never NULL.
    """

    structure_attributes = ("left", "right")

    def __init__(self, left: Term, right: Term, alias: str | None = None) -> None:
        super().__init__(alias=alias)
        self.left = left
        self.right = right

    def get_children(self) -> Iterator[Node]:
        yield self.right
        yield self.left

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = sql_context.dialect.renderers.get_distinct_from_sql(
            self.left.get_sql(sql_context), self.right.get_sql(sql_context)
        )
        return sql_context.format_alias_sql(sql, self.alias)
