from __future__ import annotations

from collections.abc import Iterator

from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term


class InfixOperator(Term):
    """An infix operator between two terms - ``(left<operator>right)``, the operator written with
    its own spacing - for the operators of a PostgreSQL extension that are no function call and no
    comparison: text search's combinations."""

    def __init__(self, left: Term, operator: str, right: Term) -> None:
        super().__init__()
        self.left = left
        self.operator = operator
        self.right = right

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.left.nodes_()
        yield from self.right.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return self.left.is_aggregate or self.right.is_aggregate

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = f"({self.left.get_sql(sql_context)}{self.operator}{self.right.get_sql(sql_context)})"
        if sql_context.with_alias and self.alias:  # pragma: nocoverage
            return f'{sql} "{self.alias}"'
        return sql
