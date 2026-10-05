from __future__ import annotations

from typing import Any, ClassVar

from hare.dialects.postgresql.functions.statistics.statistic_pair_aggregate import StatisticPairAggregate
from hare.query.expressions import (
    Exists,
    Expression,
    F,
    Q,
)
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType


class CovarPop(StatisticPairAggregate):
    """COVAR_POP(y, x) - the population covariance, or COVAR_SAMP with ``sample=True``.

    Args:
        y: The dependent field name or expression.
        x: The independent field name or expression.
        sample: The sample covariance instead of the population one.
        distinct: Only distinct ``(y, x)`` pairs.
        _filter: Only the rows matching this condition.
    """

    #: Its options are part of the key (``get_plan_options()``).
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *StatisticPairAggregate.plan_parts,
        ("sample", PlanPartType.NONE),
    )

    def __init__(
        self,
        y: str | F | Expression,
        x: str | F | Expression,
        *,
        sample: bool = False,
        distinct: bool = False,
        _filter: Q | Exists | None = None,
    ) -> None:
        super().__init__(y, x, distinct=distinct, _filter=_filter)
        self.sample = sample

    def get_function_name(self) -> str:
        return "COVAR_SAMP" if self.sample else "COVAR_POP"

    def get_plan_options(self) -> tuple[Any, ...]:
        return (*super().get_plan_options(), self.sample)
