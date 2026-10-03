from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.sql.terms.functions.analytic_function import AnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.query.expressions import ExpressionContext
    from hare.query.expressions.base.expression_result import TableCriterionTuple
from hare.query.functions.window.window_function import WindowFunction


class DistributionWindowFunction(WindowFunction):
    """A row's relative position in its partition, a float from 0 to 1."""

    def build(
        self, expression_context: ExpressionContext
    ) -> tuple[AnalyticFunction, list[TableCriterionTuple], "Field[Any] | None"]:
        return self.get_analytic_term(), [], NumericTyping.FLOAT_OUTPUT_FIELD  # type: ignore[arg-type]
