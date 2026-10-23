from __future__ import annotations

from typing import ClassVar

from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.search.constants import TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE
from hare.search.search_features import SearchFeatures
from hare.search.vector.search_vector_combinable import SearchVectorCombinable


class CombinedSearchVector(SearchVectorCombinable, Expression):
    """Two search vectors concatenated with ``+`` - needs
    ``features.supports_text_search_configurations``.

    Args:
        left: The left vector.
        right: The right vector.
    """

    plan_parts: ClassVar[DeclaredPlanParts] = (("left", PlanPartType.EXPRESSION), ("right", PlanPartType.EXPRESSION))

    def __init__(self, left: SearchVectorCombinable, right: SearchVectorCombinable) -> None:
        self.left = left
        self.right = right

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        SearchFeatures.raise_if_unsupported(
            expression_context, "SearchVector + SearchVector", TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE
        )
        return expression_context.dialect.text_search.get_combined_vector_result(self, expression_context)
