from __future__ import annotations

from typing import ClassVar

from hare.exceptions import ConfigurationError
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.search.constants import TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE
from hare.search.enums import SearchType
from hare.search.query.declarations import RawSearchQueryText
from hare.search.query.search_query_combinable import SearchQueryCombinable
from hare.search.search_features import SearchFeatures
from hare.search.types import ConfigInput, QueryInput


class SearchQuery(SearchQueryCombinable, Expression):
    """A full-text search query - ``field__search=SearchQuery(...)``, the query of ``SearchRank``
    and ``SearchHeadline``. Queries combine with ``&``, ``|`` and ``~``; each dialect writes the
    query its own way - PostgreSQL a tsquery, SQLite an FTS5 query of the model's
    ``FullTextIndex``, where ``~`` negates only the right side of ``&``.

    Args:
        value: The search text, an expression giving it, or a ``RawSearchQueryText`` (PostgreSQL's
            lexemes) - read as ``SearchType.RAW``.
        config: The text search configuration (``"english"``) - needs
            ``features.supports_text_search_configurations``.
        search_type: How the text is read - ``SearchType.PLAIN`` (every word) by default.
        invert: Negate the query.

    Raises:
        ConfigurationError: ``search_type`` isn't a ``SearchType``.
    """

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("search_type", PlanPartType.KEY),
        ("invert", PlanPartType.KEY),
        ("value", PlanPartType.ARGUMENT),
        ("config", PlanPartType.ARGUMENT),
    )

    def __init__(
        self,
        value: QueryInput,
        config: ConfigInput | None = None,
        search_type: SearchType | str = SearchType.PLAIN,
        invert: bool = False,
    ) -> None:
        if isinstance(value, RawSearchQueryText):
            self.search_type = SearchType.RAW
        else:
            try:
                self.search_type = SearchType(search_type)
            except ValueError:
                raise ConfigurationError(
                    f"search_type must be one of {[member.value for member in SearchType]}, got {search_type!r}"
                ) from None
        self.value = value
        self.config = config
        self.invert = invert

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        if self.config is not None:
            SearchFeatures.raise_if_unsupported(
                expression_context, "SearchQuery(config=...)", TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE
            )
        if isinstance(self.value, RawSearchQueryText):
            SearchFeatures.raise_if_unsupported(
                expression_context,
                f"SearchQuery({type(self.value).__name__})",
                TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE,
            )
        return expression_context.dialect.text_search.get_query_result(self, expression_context)

    def __invert__(self) -> SearchQuery:
        return SearchQuery(self.value, config=self.config, search_type=self.search_type, invert=not self.invert)
