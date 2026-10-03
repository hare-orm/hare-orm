from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.terms.base.node import TNode

if TYPE_CHECKING:
    pass
from hare.sql.terms.base.term import Term


class InfixOperator(Term):
    """An infix operator between two terms - ``(left<operator>right)``, the operator written with
    its own spacing - for the operators of a PostgreSQL extension that are no function call and no
    comparison: pgvector's distances, text search's combinations."""

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

    def get_sql(self, ctx: SqlContext) -> str:
        sql = f"({self.left.get_sql(ctx)}{self.operator}{self.right.get_sql(ctx)})"
        if ctx.with_alias and self.alias:  # pragma: nocoverage
            return f'{sql} "{self.alias}"'
        return sql
