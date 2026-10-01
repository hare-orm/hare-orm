from __future__ import annotations

from hare.dialects.postgresql.constants import SEARCH_TYPE_FUNCTIONS
from hare.dialects.postgresql.search.criterion.search_arguments import SearchArguments
from hare.dialects.postgresql.search.criterion.ts_query_function import TsQueryFunction
from hare.dialects.postgresql.search.criterion.ts_query_invert import TsQueryInvert
from hare.dialects.postgresql.search.enums import SearchType
from hare.dialects.postgresql.search.lexeme_combinable import LexemeCombinable
from hare.dialects.postgresql.search.query.search_query_combinable import SearchQueryCombinable
from hare.dialects.postgresql.search.types import ConfigInput, QueryInput
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.base.term import Term


class SearchQuery(SearchQueryCombinable, Expression):
    """Builds a tsquery expression (``PLAINTO_TSQUERY``/``PHRASETO_TSQUERY``/``TO_TSQUERY``/
    ``WEBSEARCH_TO_TSQUERY``), for use in a `SearchCriterion` or `SearchRank`.

    Args:
        value: The search text, or a `Lexeme`/`LexemeCombinable` (forces `search_type` to
            ``SearchType.RAW``).
        config: The text search configuration name (e.g. ``"english"``).
        search_type: One of the `SearchType` values.
        invert: Negate the resulting tsquery.

    Raises:
        ValueError: If `search_type` is not one of the recognized values.
    """

    def __init__(
        self,
        value: QueryInput,
        config: ConfigInput | None = None,
        search_type: SearchType | str = SearchType.PLAIN,
        invert: bool = False,
    ) -> None:
        self.search_type = SearchType.RAW if isinstance(value, LexemeCombinable) else SearchType(search_type)
        self.value = value
        self.config = config
        self.invert = invert

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The query function, the search text and the configuration, each bound.

        Args:
            context: The context the query is resolved in.

        Returns:
            The description, None for a text or a configuration given as a SQL term.
        """
        return PlanDescription.combine(
            (SearchQuery, self.search_type, self.invert),
            (
                SearchArguments.get_plan_description(self.value, context, treat_str_as_field=False),
                PlanDescription.ABSENT
                if self.config is None
                else SearchArguments.get_plan_description(self.config, context, treat_str_as_field=False),
            ),
        )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        value_result = SearchArguments.get_result(self.value, expression_context, treat_str_as_field=False)
        joins = value_result.joins
        config_term = None
        if self.config is not None:
            config_result = SearchArguments.get_result(self.config, expression_context, treat_str_as_field=False)
            config_term = config_result.term
            joins = ExpressionResult.dedup_joins(joins, config_result.joins)

        term: Term = TsQueryFunction(SEARCH_TYPE_FUNCTIONS[self.search_type], value_result.term, config_term)
        if self.invert:
            term = TsQueryInvert(term)
        return ExpressionResult(term=term, joins=joins)

    def __invert__(self) -> SearchQuery:
        return SearchQuery(
            self.value,
            config=self.config,
            search_type=self.search_type,
            invert=not self.invert,
        )
