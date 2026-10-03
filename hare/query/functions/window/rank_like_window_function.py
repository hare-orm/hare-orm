from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.terms.functions.analytic_function import AnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.query.expressions import ExpressionContext
    from hare.query.expressions.base.expression_result import TableCriterionTuple
from hare.query.functions.window.window_function import WindowFunction


class RankLikeWindowFunction(WindowFunction):
    def build(
        self, expression_context: ExpressionContext
    ) -> tuple[AnalyticFunction, list[TableCriterionTuple], "Field[Any] | None"]:
        return self.get_analytic_term(), [], None
