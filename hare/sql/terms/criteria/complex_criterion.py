from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.term import Term


class ComplexCriterion(BasicCriterion):
    def get_sql(self, sql_context: SqlContext) -> str:
        left_context = sql_context.copy(subcriterion=self.needs_brackets(self.left))
        right_context = sql_context.copy(subcriterion=self.needs_brackets(self.right))
        sql = f"{self.left.get_sql(left_context)} {self.comparator} {self.right.get_sql(right_context)}"

        if sql_context.subcriterion:
            return f"({sql})"

        return sql

    def needs_brackets(self, term: Term) -> bool:
        return isinstance(term, ComplexCriterion) and term.comparator != self.comparator
