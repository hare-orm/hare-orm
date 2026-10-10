from __future__ import annotations

from typing import Any, ClassVar, cast

from hare.fields.field import Field
from hare.query.expressions import Aggregate, CombinedExpression, Exists, F, Q
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import functions


class StatisticAggregate(Aggregate):
    """A population or sample statistic of the values - of integers a float on every backend, of
    Decimals an unrounded Decimal. SQLite computes it through a hare aggregate."""

    #: Its options are part of the key (``get_plan_options()``).
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *Aggregate.plan_parts,
        ("sample", PlanPartType.NONE),
    )

    #: The Postgres function of the population and of the sample statistic.
    function_names: tuple[str, str]
    populate_field_object = True

    def __init__(
        self,
        field: str | F | CombinedExpression,
        sample: bool = False,
        distinct: bool = False,
        _filter: Q | Exists | None = None,
    ) -> None:
        """
        Args:
            field: The field name or expression.
            sample: The sample statistic instead of the population one.
            distinct: Only distinct values.
            _filter: Only the rows matching this condition.
        """
        super().__init__(field, distinct=distinct, _filter=_filter)
        self.sample = sample

    def get_plan_options(self) -> tuple[Any, ...]:
        return (*super().get_plan_options(), self.sample)

    def _get_function_field(self, field: Any, *default_values: Any) -> functions.Statistic:
        function = functions.Statistic(self.function_names[self.sample], field)
        if self.distinct:
            function = function.distinct()
        return function

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        # The statistic of integers is a decimal.
        return self._cast_integer_result_to_float(
            super().get_result(expression_context),
            NumericTyping.FLOAT_OUTPUT_FIELD,  # type: ignore[arg-type]
            expression_context,
        )

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        return cast("Field[Any]", NumericTyping.get_quotient_output_field(field_object))
