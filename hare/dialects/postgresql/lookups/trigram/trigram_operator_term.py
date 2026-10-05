from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class TrigramOperatorTerm(Function):
    """``left <operator> right`` - a pg_trgm distance operator."""

    def __init__(self, operator: str, left: Term, right: Term) -> None:
        super().__init__(operator, left, right)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        left, right = self.args
        return f"({self.get_arg_sql(left, sql_context)} {self.name} {self.get_arg_sql(right, sql_context)})"
