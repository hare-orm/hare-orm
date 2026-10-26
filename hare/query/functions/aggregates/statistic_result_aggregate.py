from __future__ import annotations

from typing import Any, ClassVar, cast

from hare.fields.field import Field
from hare.query.expressions import Aggregate
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType


class StatisticResultAggregate(Aggregate):
    """An aggregate whose result is a statistic of the values - ``Avg`` and the population and sample
    statistics: of integers or Decimals an exact Decimal where the database computes one
    (``Features.computes_integer_statistics_exactly``, ``computes_decimal_statistics_exactly``), else a
    float."""

    plan_parts: ClassVar[DeclaredPlanParts] = (
        *Aggregate.plan_parts,
        # Read off the field's type on each build - the field is in the key.
        ("aggregated_field", PlanPartType.NONE),
    )
    populate_field_object = True

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves the statistic - of integers or Decimals the database's own decimal or float.

        Args:
            expression_context: Carries the model and virtual SQL table this aggregate is resolved
                against.

        Returns:
            The resolved term, joins, and output field.
        """
        self.aggregated_field: Field[Any] | None = None
        result = super().get_result(expression_context)
        return self._get_statistic_result(result, self.aggregated_field, expression_context)

    def _coerce_output_field(self, field_object: Field[Any], expression_context: ExpressionContext) -> Field[Any]:
        self.aggregated_field = field_object
        return cast("Field[Any]", NumericTyping.get_statistic_output_field(field_object, expression_context.dialect))
