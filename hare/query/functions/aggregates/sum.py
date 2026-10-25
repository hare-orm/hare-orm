from __future__ import annotations

from typing import Any

from hare.fields.field import Field
from hare.query.expressions import Aggregate
from hare.query.expressions.compatibility.constants import UNSUMMED_VALUE_TYPES
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.numeric.numeric_typing import NumericTyping


class Sum(Aggregate):
    """The sum of the values of a group, NULLs left out - NULL for a group of none: ``Sum("amount")``.
    Numbers and durations are added up."""

    function_name = "SUM"
    refused_argument_value_types = UNSUMMED_VALUE_TYPES
    populate_field_object = True

    def _coerce_output_field(self, field_object: Field[Any], expression_context: ExpressionContext) -> Field[Any]:
        return NumericTyping.get_sum_output_field(field_object)
