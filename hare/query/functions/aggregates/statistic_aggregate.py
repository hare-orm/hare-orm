from __future__ import annotations

from typing import Any, ClassVar

from hare.query.expressions import CombinedExpression, Exists, F, Q
from hare.query.expressions.compatibility.constants import NON_NUMBER_VALUE_TYPES
from hare.query.functions.aggregates.statistic_result_aggregate import StatisticResultAggregate
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import functions


class StatisticAggregate(StatisticResultAggregate):
    """A population or sample statistic of the values - by the database's own function, or a function
    of hare where the database has none."""

    #: Its options are part of the key (``get_plan_options()``).
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *StatisticResultAggregate.plan_parts,
        ("sample", PlanPartType.NONE),
    )

    #: The SQL function of the population and of the sample statistic.
    function_names: tuple[str, str]
    refused_argument_value_types = NON_NUMBER_VALUE_TYPES

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

    def get_function_term(self, field: Any, *default_values: Any) -> functions.Statistic:
        function = functions.Statistic(self.function_names[self.sample], field)
        if self.distinct:
            function = function.distinct()
        return function
