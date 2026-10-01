from __future__ import annotations

from hare.dialects.postgresql.search.lexeme_combinable import LexemeCombinable
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult


class CombinedLexeme(LexemeCombinable, Expression):
    """The result of combining two `LexemeCombinable` instances with ``|`` or ``&``."""

    def __init__(self, left: LexemeCombinable, operator: str, right: LexemeCombinable) -> None:
        self.left = left
        self.right = right
        self.operator = operator

    def _as_tsquery(self) -> str:
        return f"({self.left._as_tsquery()}{self.operator}{self.right._as_tsquery()})"

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return self.get_tsquery_result(expression_context)

    def __invert__(self) -> CombinedLexeme:
        operator = " & " if self.operator == " | " else " | "
        return CombinedLexeme(~self.left, operator, ~self.right)
