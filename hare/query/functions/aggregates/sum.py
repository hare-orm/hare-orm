from __future__ import annotations

from typing import Any, ClassVar

from hare.fields.data.numeric.big_int_field import BigIntField
from hare.fields.field import Field
from hare.query.expressions import Aggregate
from hare.query.expressions.compatibility.constants import UNSUMMED_VALUE_TYPES
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType


class Sum(Aggregate):
    """The sum of the values of a group, NULLs left out - NULL for a group of none: ``Sum("amount")``.
    Numbers and durations are added up."""

    function_name = "SUM"
    #: Whether the values summed are 64-bit integers, which the dialect may sum wider
    #: (``TermRenderers.get_widened_integer_sum_argument()``).
    sums_wide_integers = False
    refused_argument_value_types = UNSUMMED_VALUE_TYPES
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *Aggregate.plan_parts,
        # Read off the field's type on each build - the field is in the key.
        ("sums_wide_integers", PlanPartType.NONE),
        ("aggregated_field", PlanPartType.NONE),
    )
    populate_field_object = True

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves the sum - of integers summed exactly on every backend.

        Args:
            expression_context: Carries the model and virtual SQL table this aggregate is resolved
                against.

        Returns:
            The resolved term, joins, and output field.
        """
        self.sums_wide_integers = False
        self.aggregated_field: Field[Any] | None = None
        result = super().get_result(expression_context)
        if self.sums_wide_integers:
            self._widen_integer_argument(result, expression_context)
        self.use_decimal_aggregate(result.term, self.aggregated_field, expression_context)
        return result

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        # A sum of 64-bit integers can pass their range - of narrower ones, never a 64-bit sum's.
        self.sums_wide_integers = isinstance(field_object, BigIntField)
        self.aggregated_field = field_object
        return NumericTyping.get_sum_output_field(field_object)
