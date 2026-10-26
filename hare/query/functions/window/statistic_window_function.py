from __future__ import annotations

from typing import Any, ClassVar

from hare.query.expressions import Aggregate, Expression, Q
from hare.query.functions.window.statistic_result_window_function import StatisticResultWindowFunction
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import analytics
from hare.sql.terms.functions.analytic_function import AnalyticFunction


class StatisticWindowFunction(StatisticResultWindowFunction):
    """A population or sample statistic over the window."""

    #: Its options are part of the key (``get_plan_options()``).
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *StatisticResultWindowFunction.plan_parts,
        ("sample", PlanPartType.NONE),
    )

    #: The SQL function of the population and of the sample statistic.
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
