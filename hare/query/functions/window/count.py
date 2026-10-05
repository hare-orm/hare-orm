from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from hare.query import functions
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.expressions import ExpressionContext
    from hare.query.expressions.expression_result import ExpressionResult
from hare.query.functions.window.field_window_function import FieldWindowFunction


class Count(FieldWindowFunction):
    """``COUNT(field)`` computed over the window."""

    function_name = AnalyticFunctionName.COUNT
    analytic_class = WindowFrameAnalyticFunction
    aggregate_class = functions.Count
    accepts_encrypted_field = True

    def _get_field_result(self, expression_context: ExpressionContext) -> tuple[ExpressionResult, Field[Any] | None]:
        """The counted field - a key of several columns (``pk``, or a relation to one) counted by its
        first column, NULL exactly when there's no key, as ``Count`` counts it.

        Args:
            expression_context: The query's resolve context.

        Returns:
            The resolved field and the field describing its values.
        """
        field = self.field
        if isinstance(field, str) and field not in expression_context.annotations:
            component_paths = ConcreteFieldPaths.get_paths(expression_context.model, field)
            if len(component_paths) > 1:
                counted = copy(self)
                counted.field = component_paths[0]
                return FieldWindowFunction._get_field_result(counted, expression_context)
        return super()._get_field_result(expression_context)

    def _coerce_output_field(self, field_object: Field[Any] | None) -> Field[Any] | None:
        """A count is an integer whatever the counted field's type.

        Args:
            field_object: The counted field.

        Returns:
            None - the driver already returns a plain int.
        """
        return None
