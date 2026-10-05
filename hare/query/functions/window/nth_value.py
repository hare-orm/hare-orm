from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.query.expressions import Expression
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.analytic_function import AnalyticFunction
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.expressions import ExpressionContext
    from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.functions.window.window_function import WindowFunction


class NthValue(WindowFunction):
    """``NTH_VALUE(field, nth)`` - the field's value on the partition's ``nth`` row (1-based), over
    the whole partition like ``LastValue``; NULL when the partition has fewer rows.

    Args:
        field: A field name or an expression.
        nth: The row, from 1.
    """

    accepts_encrypted_field = True
    full_partition_frame = True

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("get_plan_options", PlanPartType.KEY_METHOD),
        ("field", PlanPartType.FIELD),
        ("nth", PlanPartType.NONE),
    )

    def __init__(self, field: str | Expression, nth: int = 1) -> None:
        if isinstance(nth, bool) or not isinstance(nth, int) or nth < 1:
            raise QueryError(f"NthValue() nth must be an integer from 1, got {nth!r}")
        self.field = field
        self.nth = nth

    def build(
        self, expression_context: ExpressionContext
    ) -> tuple[AnalyticFunction, list[TableCriterionTuple], Field[Any] | None]:
        field_result, value_field = self._get_field_result(expression_context)
        term = WindowFrameAnalyticFunction(
            AnalyticFunctionName.NTH_VALUE, field_result.term, ValueWrapper(self.nth, allow_parametrize=False)
        )
        return term, field_result.joins, value_field

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.nth,)
