from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.analytic_function import AnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.expressions import ExpressionContext
    from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.functions.window.window_function import WindowFunction


class NTile(WindowFunction):
    """``NTILE(buckets)`` - splits the partition into ``buckets`` equal-sized groups.

    Args:
        buckets: Number of groups to split the partition into.
    """

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("get_plan_options", PlanPartType.KEY_METHOD),
        ("buckets", PlanPartType.ARGUMENT),
    )

    def __init__(self, buckets: int) -> None:
        self.buckets = buckets

    def build(
        self, expression_context: ExpressionContext
    ) -> tuple[AnalyticFunction, list[TableCriterionTuple], Field[Any] | None]:
        buckets = ExpressionArguments.get_result(self, "buckets", self.buckets, expression_context)
        return AnalyticFunction(AnalyticFunctionName.NTILE, buckets.term), list(buckets.joins), None
