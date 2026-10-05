from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.exceptions import (
    QueryError,
)
from hare.query.expressions import Q
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.conditions.query_modifier import QueryModifier
    from hare.query.expressions.expression_context import ExpressionContext


class UnsavedOwnerCondition(Q):
    """The condition of a relation read off an instance that isn't saved: it has no key the
    related rows could reference, so whatever is queried through it is refused."""

    __slots__ = ()

    MESSAGE: ClassVar[str] = "This objects hasn't been instanced, call .save() before calling related queries"

    plan_parts: ClassVar[DeclaredPlanParts] = (("keeps_plan", PlanPartType.KEEPS_PLAN_METHOD), *Q.plan_parts)

    def keeps_plan(self) -> bool:
        """Refuses the query - describing it is the first thing a query does with its conditions.

        Raises:
            QueryError: Always.
        """
        raise QueryError(self.MESSAGE)

    def get_result(self, expression_context: ExpressionContext) -> QueryModifier:
        raise QueryError(self.MESSAGE)
