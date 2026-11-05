from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.execution.query_execution import QueryExecution
from hare.query.expressions import Q

if TYPE_CHECKING:
    from hare.query.execution.query_dependency import QueryDependency
    from hare.query.expressions.conditions.query_modifier import QueryModifier
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.query.plans.description.plan_context import PlanContext
    from hare.query.plans.description.plan_description import PlanDescription


class DependentCondition(Q):
    """A condition whose values are supplied by an execution dependency.

    The declaration has stable identity. Description and compilation read the same ordinary
    Q from the execution, so cached plans contain the usual parameter origins and no IO state.
    """

    __slots__ = ("dependency",)

    def __init__(self, dependency: QueryDependency) -> None:
        super().__init__()
        self.dependency = dependency

    def __bool__(self) -> bool:
        return True

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DependentCondition) and self.get_structure_key() == other.get_structure_key()

    def __hash__(self) -> int:
        return hash(self.get_structure_key())

    def __invert__(self) -> Q:
        return ~Q(self)

    def get_structure_key(self) -> tuple[Any, ...]:
        return (type(self), self.dependency, self._is_negated, self._needs_double_negation_rewrite)

    def _get_bound_condition(self) -> Q:
        condition = QueryExecution.get_condition(self.dependency)
        if not self._is_negated and not self._filter_call_generation:
            return condition
        bound = condition._with_filter_call_generation(self._filter_call_generation)
        bound._is_negated = self._is_negated
        bound._needs_double_negation_rewrite = self._needs_double_negation_rewrite
        return bound

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        return self._get_bound_condition().get_plan_description(context)

    def get_result(self, expression_context: ExpressionContext) -> QueryModifier:
        return self._get_bound_condition().get_result(expression_context)
