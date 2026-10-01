from __future__ import annotations

from hare.query.expressions.base.arithmetic_expression_mixin import ArithmeticExpressionMixin
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.sql.terms.base.term import Term


class TermExpression(ArithmeticExpressionMixin):
    """A raw SQL term (``RawSQL(...)``, a ``hare.sql`` term) used as an expression - embedded in the
    SQL as is, never bound as a parameter.

    Args:
        term: The term.
    """

    plannable = False

    def __init__(self, term: Term) -> None:
        self.term = term

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return ExpressionResult(term=self.term)
