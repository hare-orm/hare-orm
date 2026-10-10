from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from hare.exceptions import ConfigurationError
from hare.numbers.finite_numbers import FiniteNumbers
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, F
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.search.constants import (
    FULL_TEXT_INDEX_REQUIRED_FEATURE,
    SEARCH_RANK_MAX_FIELD_WEIGHT,
    TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE,
)
from hare.search.query.search_query import SearchQuery
from hare.search.search_features import SearchFeatures
from hare.search.types import NormalizationInput, QueryInput, RankVectorInput, RankWeightInput
from hare.search.vector.search_vector import SearchVector
from hare.sql.terms.term import Term


class SearchRank(Expression):
    """How well each row matches a full-text search, higher for a better match - for an annotation
    to order the rows by. PostgreSQL ranks a tsvector with ``ts_rank()``; SQLite ranks the fields of
    the model's ``FullTextIndex`` with FTS5's ``bm25()``, 0 for a row the query doesn't match.

    Example: ``Article.objects.annotate(rank=SearchRank(("title", "body"), "hare orm"))
    .filter(rank__gt=0).order_by("-rank")``

    Args:
        vector: The ranked field, several fields, a ``SearchVector``, or (on PostgreSQL) a tsvector
            expression.
        query: The search text (``SearchType.PLAIN``) or a ``SearchQuery``.
        weights: A weight by field name - a match in a heavier field ranks higher, 1 for a field
            left out (needs ``features.supports_full_text_index``); or the weights of PostgreSQL's
            labels D, C, B, A as a sequence or an expression (needs
            ``features.supports_text_search_configurations``).
        normalization: How the rank is normalized by the text's length, PostgreSQL's bitmask - needs
            ``features.supports_text_search_configurations``.
        cover_density: Rank by cover density (``ts_rank_cd()``) - needs
            ``features.supports_text_search_configurations``.

    Raises:
        ConfigurationError: No fields, or a field weight that isn't a finite number from 0 to
            1000000.
    """

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
                if not FiniteNumbers.is_finite_number(weight) or not 0 <= weight <= SEARCH_RANK_MAX_FIELD_WEIGHT:
                    raise ConfigurationError(
                        f"SearchRank weight of {field_name!r} must be a number from 0 to "
                        f"{SEARCH_RANK_MAX_FIELD_WEIGHT:g}, got {weight!r}"
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
        if isinstance(self.weights, dict):
            SearchFeatures.raise_if_unsupported(
                expression_context, "SearchRank(weights={field: weight})", FULL_TEXT_INDEX_REQUIRED_FEATURE
            )
        elif self.weights is not None:
            SearchFeatures.raise_if_unsupported(
                expression_context, "SearchRank(weights=[D, C, B, A])", TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE
            )
        if self.normalization is not None:
            SearchFeatures.raise_if_unsupported(
                expression_context, "SearchRank(normalization=...)", TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE
            )
        if self.cover_density:
            SearchFeatures.raise_if_unsupported(
                expression_context, "SearchRank(cover_density=True)", TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE
            )
        return expression_context.dialect.text_search.get_rank_result(self, expression_context)
