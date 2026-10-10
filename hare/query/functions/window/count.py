from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query import functions
from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
from hare.query.functions.window.field_window_function import FieldWindowFunction


class Count(FieldWindowFunction):
    """``COUNT(field)`` computed over the window."""

    function_name = AnalyticFunctionName.COUNT
    analytic_class = WindowFrameAnalyticFunction
    aggregate_class = functions.Count
    accepts_encrypted_field = True

    def _coerce_output_field(self, field_object: Field[Any] | None) -> Field[Any] | None:
        """A count is an integer whatever the counted field's type.

        Args:
            field_object: The counted field.

        Returns:
            None - the driver already returns a plain int.
        """
        return None
