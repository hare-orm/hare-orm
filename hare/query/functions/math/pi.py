from __future__ import annotations

from hare.query.expressions.declarations import ConstantExpression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.sql.functions.declarations import MathFunction as MathFunctionTerm


class Pi(ConstantExpression):
    """The number pi, as a float."""

    populate_field_object = True

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return ExpressionResult(
            term=MathFunctionTerm("PI"),
            output_field=NumericTyping.FLOAT_OUTPUT_FIELD,  # type: ignore[arg-type]
        )

    value_field = NumericTyping.FLOAT_OUTPUT_FIELD  # type: ignore[arg-type]
