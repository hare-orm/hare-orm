from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.query.expressions import Aggregate, Expression, Q
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import analytics
from hare.sql.terms.functions.analytic_function import AnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
from hare.query.functions.window.field_window_function import FieldWindowFunction


class StatisticWindowFunction(FieldWindowFunction):
    """A population or sample statistic over the window - of integers a float, of Decimals an
    unrounded Decimal."""

    #: Its options are part of the key (``get_plan_options()``).
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *FieldWindowFunction.plan_parts,
        ("sample", PlanPartType.NONE),
    )

    #: The Postgres function of the population and of the sample statistic.
    function_names: ClassVar[tuple[str, str]]

    def __init__(self, field: str | Expression, condition: Q | None = None, sample: bool = False) -> None:
        super().__init__(field, condition)
        self.sample = sample

    def copy_aggregate_options(self, aggregate: Aggregate) -> None:
        self.sample = getattr(aggregate, "sample", False)

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.sample,)

    def get_analytic_term(self, *args: Any) -> AnalyticFunction:
        return analytics.Statistic(self.function_names[self.sample], *args)

    def _coerce_output_field(self, field_object: Field[Any] | None) -> Field[Any] | None:
        return NumericTyping.get_quotient_output_field(field_object)
