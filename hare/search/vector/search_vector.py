from __future__ import annotations

from typing import ClassVar

from hare.exceptions import QueryError
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.search.constants import TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE
from hare.search.search_features import SearchFeatures
from hare.search.types import ConfigInput, VectorInput, WeightInput
from hare.search.vector.search_vector_combinable import SearchVectorCombinable


class SearchVector(SearchVectorCombinable, Expression):
    """The searchable text of one or more fields or expressions, concatenated. As the vector of
    ``SearchRank`` it names the ranked fields on every dialect; as a value of its own (an
    annotation, ``+``, a configuration, a weight) it is PostgreSQL's tsvector and needs
    ``features.supports_text_search_configurations``.

    Args:
        expressions: Field names or expressions to concatenate into the vector.
        config: The text search configuration (``"english"``).
        weight: A weight letter (``"A"``-``"D"``) given to the vector's words.

    Raises:
        QueryError: No expressions are given.
    """

    #: A weight letter is written into the SQL text, any other weight bound.
    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("expressions", PlanPartType.FIELDS),
        ("config", PlanPartType.ARGUMENT),
        ("weight", PlanPartType.NONE),
        ("weight_letter", PlanPartType.KEY),
        ("bound_weight", PlanPartType.ARGUMENT),
    )

    def __init__(
        self,
        *expressions: VectorInput,
        config: ConfigInput | None = None,
        weight: WeightInput | None = None,
    ):
        if not expressions:
            raise QueryError("SearchVector requires at least one expression.")
        self.expressions = expressions
        self.config = config
        self.weight = weight
        #: The weight written into the SQL text - a letter.
        self.weight_letter = weight if isinstance(weight, str) else None
        #: The weight bound - an expression or a SQL term giving one.
        self.bound_weight = None if weight is None or isinstance(weight, str) else weight

    def get_field_names(self) -> tuple[str, ...] | None:
        """The fields the vector concatenates as they are.

        Returns:
            Their names, None when the vector holds an expression, a configuration or a weight.
        """
        if self.config is not None or self.weight is not None:
            return None
        if not all(isinstance(expression, str) for expression in self.expressions):
            return None
        return self.expressions  # type: ignore[return-value]

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        SearchFeatures.raise_if_unsupported(
            expression_context, "SearchVector", TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE
        )
        return expression_context.dialect.text_search.get_vector_result(self, expression_context)
