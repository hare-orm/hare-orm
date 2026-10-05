from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.query.functions.window.field_window_function import FieldWindowFunction
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.expressions import Aggregate, ExpressionContext
    from hare.query.expressions.expression_result import TableCriterionTuple
    from hare.sql.terms.functions import AggregateFunction
    from hare.sql.terms.functions.analytic_function import AnalyticFunction


class AggregateWindowFunction(FieldWindowFunction):
    """An aggregate computed over the window as the aggregate itself builds it - its own function,
    extra arguments and ``_filter=`` (``Window(Quantile("price", 0.9))``): what ``Window(...)`` makes
    of an aggregate declaring ``computed_over_window``.

    Args:
        aggregate: The aggregate.
    """

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("aggregate", PlanPartType.EXPRESSION),
        ("field", PlanPartType.NONE),
        ("condition", PlanPartType.NONE),
    )

    def __init__(self, aggregate: Aggregate) -> None:
        super().__init__(cast("Any", aggregate.field), aggregate.filter)
        self.aggregate = aggregate

    def build(
        self, expression_context: ExpressionContext
    ) -> tuple[AnalyticFunction, list[TableCriterionTuple], Field[Any] | None]:
        result = self.aggregate.get_result(expression_context)
        analytic_function = self.aggregate.get_analytic_function(cast("AggregateFunction", result.term))
        return analytic_function, result.joins, self.aggregate.get_value_field(result)
