from __future__ import annotations

from typing import Any, ClassVar, cast

from hare.fields.data.numeric.big_int_field import BigIntField
from hare.fields.field import Field
from hare.query.expressions import Aggregate
from hare.query.expressions.compatibility.constants import UNSUMMED_VALUE_TYPES
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType


class Avg(Aggregate):
    """The mean of the values of a group, NULLs left out - NULL for a group of none: ``Avg("price")``.
    An average of integers is a decimal."""

    function_name = "AVG"
    #: Whether the values averaged are 64-bit integers - see ``Sum.sums_wide_integers``.
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
        """Resolves the average - of integers a float on every backend.

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
        # An average of integers is a decimal - a Decimal inside arithmetic or text.
        return self._cast_integer_result_to_float(
            result,
            NumericTyping.FLOAT_OUTPUT_FIELD,
            expression_context,
        )

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        # The sum of 64-bit integers it divides can pass their range.
        self.sums_wide_integers = isinstance(field_object, BigIntField)
        self.aggregated_field = field_object
        return cast("Field[Any]", NumericTyping.get_quotient_output_field(field_object))
