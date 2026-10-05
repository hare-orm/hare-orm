from __future__ import annotations

from typing import ClassVar

from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.search.enums import SearchOperator
from hare.search.query.search_query_combinable import SearchQueryCombinable


class CombinedSearchQuery(SearchQueryCombinable, Expression):
    """Two search queries combined with ``&`` or ``|``.

    Args:
        left: The left query.
        operator: How the queries are joined.
        right: The right query.
        negated: Negate the combination.
    """

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("operator", PlanPartType.KEY),
        ("negated", PlanPartType.KEY),
        ("left", PlanPartType.EXPRESSION),
        ("right", PlanPartType.EXPRESSION),
    )

    def __init__(
        self,
        left: SearchQueryCombinable,
        operator: SearchOperator,
        right: SearchQueryCombinable,
        *,
        negated: bool = False,
    ) -> None:
        self.left = left
        self.right = right
        self.operator = operator
        self.negated = negated

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return expression_context.dialect.text_search.get_combined_query_result(self, expression_context)

    def __invert__(self) -> CombinedSearchQuery:
        return CombinedSearchQuery(self.left, self.operator, self.right, negated=not self.negated)
