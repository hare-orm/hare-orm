from __future__ import annotations

from enum import Enum

from hare.sql.constants import BRACKETED_CRITERION_DEPTH
from hare.sql.enums import Comparator, JSONOperators
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.term import Term


class ComplexCriterion(BasicCriterion):
    """Two conditions joined by AND, OR or XOR."""

    def __init__(
        self,
        comparator: Comparator | JSONOperators | Enum,
        left: Term,
        right: Term,
        alias: str | None = None,
    ) -> None:
        super().__init__(comparator, left, right, alias)
        #: How many criteria of criteria this one nests - a deep one of this connector is written in
        #: brackets, so that a database parsing a chain as deep as it is long
        #: (``Features.max_expression_depth``) reads it as deep as the criterion is.
        self.depth: int = 1 + max(
            left.depth if isinstance(left, ComplexCriterion) else 0,
            right.depth if isinstance(right, ComplexCriterion) else 0,
        )

    def get_sql(self, sql_context: SqlContext) -> str:
        left_context = sql_context.copy(subcriterion=self.needs_brackets(self.left))
        right_context = sql_context.copy(subcriterion=self.needs_brackets(self.right))
        sql = f"{self.left.get_sql(left_context)} {self.comparator} {self.right.get_sql(right_context)}"

        if sql_context.with_alias and self.alias:
            # Selected under a name - a condition a derived table reads back by it.
            return sql_context.format_alias_sql(f"({sql})", self.alias)
        if sql_context.subcriterion:
            return f"({sql})"

        return sql

    def needs_brackets(self, term: Term) -> bool:
        return isinstance(term, ComplexCriterion) and (
            term.comparator != self.comparator or term.depth >= BRACKETED_CRITERION_DEPTH
        )
