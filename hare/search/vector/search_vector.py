from __future__ import annotations

from typing import ClassVar

from hare.exceptions import ConfigurationError, QueryError
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.expressions.structure.enums import ArgumentRole
from hare.query.expressions.structure.expression_structure import ExpressionStructure
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.search.messages import GENERATED_VECTOR_CONFIG_REQUIRED
from hare.search.types import ConfigInput, VectorInput, WeightInput
from hare.search.vector.search_vector_combinable import SearchVectorCombinable


class SearchVector(SearchVectorCombinable, Expression):
    """The searchable text of one or more fields or expressions, concatenated. As the vector of
    ``SearchRank`` it names the ranked fields on every dialect; as a value of its own (an
    annotation, ``+``, a configuration, a weight) it is the database's own vector and needs
    ``features.text_search_operations.vector``.

    Args:
        expressions: Field names or expressions to concatenate into the vector.
        config: The text search configuration (``"english"``).
        weight: A weight letter (``"A"``-``"D"``) given to the vector's words.

    Raises:
        QueryError: No expressions are given.
    """

    structure = ExpressionStructure(positional_role=ArgumentRole.FIELD)

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
        vector = self
        if self.config is None:
            if expression_context.requires_search_config and expression_context.text_search_config is None:
                raise ConfigurationError(GENERATED_VECTOR_CONFIG_REQUIRED)
            if expression_context.text_search_config is not None:
                vector = SearchVector(
                    *self.expressions, config=expression_context.text_search_config, weight=self.weight
                )
        return expression_context.dialect.text_search.get_vector_result(vector, expression_context)
