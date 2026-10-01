from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:
    pass
from hare.sql.terms.criteria.basic_criterion import BasicCriterion


class ComplexCriterion(BasicCriterion):
    def get_sql(self, ctx: SqlContext) -> str:
        left_ctx = ctx.copy(subcriterion=self.needs_brackets(self.left))
        right_ctx = ctx.copy(subcriterion=self.needs_brackets(self.right))
        sql = f"{self.left.get_sql(left_ctx)} {self.comparator} {self.right.get_sql(right_ctx)}"

        if ctx.subcriterion:
            return f"({sql})"

        return sql

    def needs_brackets(self, term: Term) -> bool:
        return isinstance(term, ComplexCriterion) and not term.comparator == self.comparator
