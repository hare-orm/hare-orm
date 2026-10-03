from __future__ import annotations

from hare.dialects.postgresql.search.criterion.declarations import TsInfixOperator
from hare.dialects.postgresql.search.criterion.ts_query_invert import TsQueryInvert
from hare.dialects.postgresql.search.query.search_query_combinable import SearchQueryCombinable
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.base.term import Term


class CombinedSearchQuery(SearchQueryCombinable, Expression):
    """The result of combining two `SearchQueryCombinable` instances with ``|`` or ``&``."""

    def __init__(
        self, left: SearchQueryCombinable, operator: str, right: SearchQueryCombinable, *, negated: bool = False
    ) -> None:
        self.left = left
        self.right = right
        self.operator = operator
        self.negated = negated

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The operator and both queries' descriptions.

        Args:
            context: The context the query is resolved in.

        Returns:
            The description, None when a query keeps no plan.
        """
        return PlanDescription.combine(
            (CombinedSearchQuery, self.operator, self.negated),
            (self.left.get_plan_description(context), self.right.get_plan_description(context)),
        )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        left = self.left.get_result(expression_context)
        right = self.right.get_result(expression_context)
        term: Term = TsInfixOperator(left.term, self.operator, right.term)
        if self.negated:
            term = TsQueryInvert(term)
        return ExpressionResult(term=term, joins=ExpressionResult.dedup_joins(left.joins, right.joins))

    def __invert__(self) -> CombinedSearchQuery:
        return CombinedSearchQuery(self.left, self.operator, self.right, negated=not self.negated)
