from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.query.expressions import Expression
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.functions.analytic_function import AnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.query.expressions import ExpressionContext
    from hare.query.expressions.base.expression_result import TableCriterionTuple
from hare.query.functions.window.window_function import WindowFunction


class NTile(WindowFunction):
    """``NTILE(buckets)`` - splits the partition into ``buckets`` equal-sized groups.

    Args:
        buckets: Number of groups to split the partition into.
    """

    def __init__(self, buckets: int) -> None:
        self.buckets = buckets

    def get_plan_arguments(self, context: PlanContext) -> Iterable[PlanDescription | None]:
        return (Expression.get_argument_plan_description(self.buckets, context),)

    def build(
        self, expression_context: ExpressionContext
    ) -> tuple[AnalyticFunction, list[TableCriterionTuple], "Field[Any] | None"]:
        term = AnalyticFunction(AnalyticFunctionName.NTILE, self.buckets)
        # The bucket count is wrapped inside the SQL function's constructor, not through Value -
        # recorded here.
        if expression_context.value_wrapper_refs is not None and isinstance(term.args[0], ValueWrapper):
            expression_context.value_wrapper_refs.append((ValueRefOrigin.ANNOTATION, LiteralValueRef(term.args[0])))
        return term, [], None
