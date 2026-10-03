from __future__ import annotations

from hare.dialects.postgresql.fields.search import TSVectorField
from hare.dialects.postgresql.search.criterion.declarations import TsInfixOperator
from hare.dialects.postgresql.search.vector.search_vector_combinable import SearchVectorCombinable
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription


class CombinedSearchVector(SearchVectorCombinable, Expression):
    """The result of concatenating two `SearchVectorCombinable` instances with ``+``."""

    def __init__(self, left: SearchVectorCombinable, right: SearchVectorCombinable) -> None:
        self.left = left
        self.right = right

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        return PlanDescription.combine(
            CombinedSearchVector, (self.left.get_plan_description(context), self.right.get_plan_description(context))
        )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        left = self.left.get_result(expression_context)
        right = self.right.get_result(expression_context)
        # left/right are each already a NULL-safe TO_TSVECTOR(...) result (SearchVector.get_result()
        # guards its own field concatenation with COALESCE before that point), so no additional
        # guard is needed here.
        term = TsInfixOperator(left.term, " || ", right.term)
        return ExpressionResult(
            term=term,
            joins=ExpressionResult.dedup_joins(left.joins, right.joins),
            output_field=TSVectorField(),
        )
