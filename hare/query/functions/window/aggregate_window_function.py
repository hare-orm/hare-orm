from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.query.expressions.structure.expression_structure import ExpressionStructure
from hare.query.functions.window.window_function import WindowFunction
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.query.expressions import Aggregate, ExpressionContext
    from hare.query.expressions.expression_result import TableCriterionTuple
    from hare.sql.terms.functions.analytic_function import AnalyticFunction


class AggregateWindowFunction(WindowFunction):
    """An aggregate computed over the window as the aggregate itself builds it - its own function,
    extra arguments and ``_filter=`` (``Window(Quantile("price", 0.9))``): what ``Window(...)`` makes
    of an aggregate declaring ``computed_over_window``.

    Args:
        aggregate: The aggregate.
    """

    structure = ExpressionStructure()

    plan_parts: ClassVar[DeclaredPlanParts] = (("aggregate", PlanPartType.EXPRESSION),)

    def __init__(self, aggregate: Aggregate) -> None:
        # The aggregate checked its own argument and filter - it computes them itself.
        self.aggregate = aggregate

    def build(
        self, expression_context: ExpressionContext
    ) -> tuple[AnalyticFunction, list[TableCriterionTuple], Field[Any] | None]:
        result = self.aggregate.get_result(expression_context)
        analytic_function = self.aggregate.get_analytic_function(result.term)  # type: ignore[arg-type]
        return analytic_function, result.joins, self.aggregate.get_value_field(result)
