from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.expressions.numeric.numeric_typing import NumericTyping

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.query.expressions.expression_context import ExpressionContext
from hare.query.functions.window.field_window_function import FieldWindowFunction


class StatisticResultWindowFunction(FieldWindowFunction):
    """A window function whose result is a statistic of the values - ``Avg`` and the population and
    sample statistics: of integers or Decimals an exact Decimal where the database computes one, else a
    float."""

    def _coerce_output_field(
        self, field_object: Field[Any] | None, expression_context: ExpressionContext
    ) -> Field[Any] | None:
        return NumericTyping.get_statistic_output_field(field_object, expression_context.dialect)
