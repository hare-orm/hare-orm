from __future__ import annotations

from hare.query.expressions.declarations import ConstantExpression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.sql.functions.random_number import RandomNumber


class Random(ConstantExpression):
    """A random float from 0 (included) to 1 (excluded), new for every row - ``order_by("?")``
    orders by it."""

    populate_field_object = True

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return ExpressionResult(
            term=RandomNumber(),
            output_field=NumericTyping.FLOAT_OUTPUT_FIELD,  # type: ignore[arg-type]
        )

    value_field = NumericTyping.FLOAT_OUTPUT_FIELD  # type: ignore[arg-type]
