from __future__ import annotations

from collections.abc import Iterator

from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term


class InfixOperator(Term):
    """An infix operator between two terms - ``(left<operator>right)``, the operator written with
    its own spacing - for the operators of a dialect that are no function call and no comparison:
    text search's combinations."""

    structure_attributes = ("left", "operator", "right")

    def __init__(self, left: Term, operator: str, right: Term) -> None:
        super().__init__()
        self.left = left
        self.operator = operator
        self.right = right

    def get_children(self) -> Iterator[Node]:
        yield self.left
        yield self.right

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return Term.get_combined_is_aggregate([self.left.is_aggregate, self.right.is_aggregate])

    def get_sql(self, sql_context: SqlContext) -> str:
        arg_context = sql_context.copy(with_alias=False)
        sql = f"({self.left.get_sql(arg_context)}{self.operator}{self.right.get_sql(arg_context)})"
        if sql_context.with_alias:
            return sql_context.format_alias_sql(sql, self.alias)
        return sql
