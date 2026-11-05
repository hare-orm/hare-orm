from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ConfigurationError
from hare.numbers.finite_numbers import FiniteNumbers
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, F
from hare.query.expressions.structure.enums import ArgumentRole
from hare.query.expressions.structure.expression_structure import ExpressionStructure
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.search.query.search_query import SearchQuery
from hare.search.types import NormalizationInput, QueryInput, RankVectorInput, RankWeightInput
from hare.search.vector.search_vector import SearchVector
from hare.sql.terms.term import Term


class SearchRank(Expression):
    """How well each row matches a full-text search, higher for a better match - for an annotation
    to order the rows by - by the database's own rank function; a row the query doesn't match gets
    what that function gives it.

    Example: ``Article.objects.annotate(rank=SearchRank(("title", "body"), "hare orm"))
    .filter(rank__gt=0).order_by("-rank")``

    Args:
        vector: The ranked field, several fields, a ``SearchVector``, or a vector
            expression supported by the dialect's text-search compiler.
        query: The search text (``SearchType.PLAIN``) or a ``SearchQuery``.
        weights: A weight by field name - a match in a heavier field ranks higher, 1 for a field
            left out (``rank_field_weights``); or weights of the labels D, C, B, A as a
            sequence or expression (``rank_label_weights``).
        normalization: Length normalization bitmask (``rank_normalization``).
        cover_density: Rank by cover density (``rank_cover_density``).

    Optional operations are checked independently in ``Features.text_search_operations``.

    Raises:
        ConfigurationError: No fields, or a field weight that isn't a finite nonnegative number.
    """

    structure = ExpressionStructure(
        positions=((0, ArgumentRole.FIELD), (2, ArgumentRole.FIELD_MAPPING)),
        keywords=(("vector", ArgumentRole.FIELD), ("weights", ArgumentRole.FIELD_MAPPING)),
    )

    #: The vector, the query, the weights and the normalization, in the order the dialect resolves
    #: them.
    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("cover_density", PlanPartType.KEY),
        ("field_weights", PlanPartType.KEY),
        ("vector", PlanPartType.NONE),
        ("get_vector_argument", PlanPartType.ARGUMENT_METHOD),
        ("query", PlanPartType.NONE),
        ("query_argument", PlanPartType.ARGUMENT),
        ("weights", PlanPartType.NONE),
        ("bound_weights", PlanPartType.ENCODED_ARGUMENT),
        ("normalization", PlanPartType.ARGUMENT),
    )

    def __init__(
        self,
        vector: RankVectorInput,
        query: QueryInput,
        weights: RankWeightInput | Mapping[str, float] | None = None,
        normalization: NormalizationInput | None = None,
        cover_density: bool = False,
    ) -> None:
        if not isinstance(vector, (str, Expression, Term)):
            if not isinstance(vector, Sequence) or not vector or not all(isinstance(name, str) for name in vector):
                raise ConfigurationError(f"SearchRank fields must be one or more field names, got {vector!r}")
            vector = tuple(vector)
        if isinstance(weights, Mapping):
            for field_name, weight in weights.items():
                if not FiniteNumbers.is_finite_number(weight) or weight < 0:
                    raise ConfigurationError(
                        f"SearchRank weight of {field_name!r} must be a finite nonnegative number, "
                        f"got {ValueDescriptions.get_shown_value(weight)}"
                    )
            weights = dict(weights)
        self.vector = vector
        self.query = query
        #: The query as an expression - search text as a ``SearchQuery``.
        self.query_argument = query if isinstance(query, (Expression, Term)) else SearchQuery(query)
        self.weights = weights
        #: The weights of a ``FullTextIndex``'s fields, written into the SQL text.
        self.field_weights = tuple(sorted(weights.items())) if isinstance(weights, dict) else None
        #: The weights of the labels, bound whole.
        self.bound_weights = None if weights is None or isinstance(weights, dict) else weights
        self.normalization = normalization
        self.cover_density = cover_density

    def get_vector_argument(self) -> Any:
        """The vector as an expression - a field name as ``F()``, field names as a ``SearchVector``.

        Returns:
            The expression or term.
        """
        if isinstance(self.vector, (Expression, Term)):
            return self.vector
        if isinstance(self.vector, str):
            return F(self.vector)
        return SearchVector(*self.vector)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return expression_context.dialect.text_search.get_rank_result(self, expression_context)
