from __future__ import annotations

from typing import Any, cast

from hare.fields.field import Field
from hare.query.expressions import Aggregate
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_typing import NumericTyping


class Avg(Aggregate):
    """Returns average (mean) of all values in the column, e.g. ``Avg("field_name")``."""

    function_name = "AVG"
    populate_field_object = True

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves the average - of integers a float on every backend.

        Args:
            expression_context: Carries the model and virtual SQL table this aggregate is resolved
                against.

        Returns:
            The resolved term, joins, and output field.
        """
        # An average of integers is a decimal - a Decimal inside arithmetic or text.
        return self._cast_integer_result_to_float(
            super().get_result(expression_context),
            NumericTyping.FLOAT_OUTPUT_FIELD,  # type: ignore[arg-type]
            expression_context,
        )

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        return cast("Field[Any]", NumericTyping.get_quotient_output_field(field_object))
