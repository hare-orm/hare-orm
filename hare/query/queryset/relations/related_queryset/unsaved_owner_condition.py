from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.exceptions import (
    QueryError,
)
from hare.query.expressions import Q

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.base.expression_context import ExpressionContext
    from hare.query.expressions.modifier import QueryModifier
    from hare.query.plans.description.plan_context import PlanContext
    from hare.query.plans.description.plan_description import PlanDescription


class UnsavedOwnerCondition(Q):
    """The condition of a relation read off an instance that isn't saved: it has no key the
    related rows could reference, so whatever is queried through it is refused."""

    __slots__ = ()

    MESSAGE: ClassVar[str] = "This objects hasn't been instanced, call .save() before calling related queries"

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        raise QueryError(self.MESSAGE)

    def get_result(self, expression_context: ExpressionContext) -> QueryModifier:
        raise QueryError(self.MESSAGE)
