from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query import functions
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
from hare.query.functions.window.field_window_function import FieldWindowFunction


class Sum(FieldWindowFunction):
    """``SUM(field)`` computed over the window."""

    function_name = AnalyticFunctionName.SUM
    analytic_class = WindowFrameAnalyticFunction
    aggregate_class = functions.Sum

    def _coerce_output_field(self, field_object: Field[Any] | None) -> Field[Any] | None:
        return None if field_object is None else NumericTyping.get_sum_output_field(field_object)
