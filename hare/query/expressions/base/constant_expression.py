from __future__ import annotations

from hare.query.expressions.base.expression import Expression
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription


class ConstantExpression(Expression):
    """An expression whose SQL text takes no value - ``PI()``, the current moment."""

    def get_plan_description(self, context: PlanContext) -> PlanDescription:
        return PlanDescription((type(self),), [])
