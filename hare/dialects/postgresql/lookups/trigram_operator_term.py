from hare.sql.context import SqlContext
from hare.sql.terms.base.term import Term
from hare.sql.terms.functions.function import Function


class TrigramOperatorTerm(Function):
    """``left <operator> right`` - a pg_trgm distance operator."""

    def __init__(self, operator: str, left: Term, right: Term) -> None:
        super().__init__(operator, left, right)

    def get_function_sql(self, ctx: SqlContext) -> str:
        left, right = self.args
        return f"({self.get_arg_sql(left, ctx)} {self.name} {self.get_arg_sql(right, ctx)})"
