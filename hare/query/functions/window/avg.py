from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query import functions
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
from hare.query.functions.window.field_window_function import FieldWindowFunction


class Avg(FieldWindowFunction):
    """``AVG(field)`` computed over the window."""

    function_name = AnalyticFunctionName.AVG
    analytic_class = WindowFrameAnalyticFunction
    aggregate_class = functions.Avg

    def _coerce_output_field(self, field_object: "Field[Any] | None") -> "Field[Any] | None":
        return NumericTyping.get_quotient_output_field(field_object)
