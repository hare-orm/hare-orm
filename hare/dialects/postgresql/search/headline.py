from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.search.criterion.search_arguments import SearchArguments
from hare.dialects.postgresql.search.query.search_query import SearchQuery
from hare.dialects.postgresql.search.types import ConfigInput, HeadlineExpressionInput, HeadlineOptionValue, QueryInput
from hare.fields import TextField
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.functions.function import Function as HareSqlFunction


class SearchHeadline(Expression):
    """Builds a ``TS_HEADLINE(...)`` expression - the source text with search-term matches
    highlighted, for use in an annotation.

    Args:
        expression: The field name/expression holding the source text.
        query: A `SearchQuery`/tsquery term, or search text to wrap in one.
        config: The text search configuration name (e.g. ``"english"``).
        start_sel: Opening highlight marker (default ``<b>``).
        stop_sel: Closing highlight marker (default ``</b>``).
        max_words: Maximum words per highlighted fragment.
        min_words: Minimum words per highlighted fragment.
        short_word: Words shorter than this many letters are dropped at fragment boundaries.
        highlight_all: Highlight the whole document instead of extracting fragments.
        max_fragments: Maximum number of fragments to return.
        fragment_delimiter: String inserted between fragments.
    """

    def __init__(
        self,
        expression: HeadlineExpressionInput,
        query: QueryInput,
        config: ConfigInput | None = None,
        start_sel: str | None = None,
        stop_sel: str | None = None,
        max_words: int | None = None,
        min_words: int | None = None,
        short_word: int | None = None,
        highlight_all: bool | None = None,
        max_fragments: int | None = None,
        fragment_delimiter: str | None = None,
    ) -> None:
        self.expression = expression
        self.query = query
        self.config = config
        self.options = {
            "StartSel": start_sel,
            "StopSel": stop_sel,
            "MaxWords": max_words,
            "MinWords": min_words,
            "ShortWord": short_word,
            "HighlightAll": highlight_all,
            "MaxFragments": max_fragments,
            "FragmentDelimiter": fragment_delimiter,
        }

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The source text, the query and the configuration, bound; the options are written into
        the SQL text.

        Args:
            context: The context the headline is resolved in.

        Returns:
            The description, None for an argument given as a SQL term.
        """
        query_argument: Any = self.query if isinstance(self.query, (Expression, Term)) else SearchQuery(self.query)
        options = tuple((key, value) for key, value in self.options.items() if value is not None)
        return PlanDescription.combine(
            (SearchHeadline, options),
            (
                PlanDescription.ABSENT
                if argument is None
                else SearchArguments.get_plan_description(argument, context, treat_str_as_field=treat_str_as_field)
                for argument, treat_str_as_field in (
                    (self.expression, True),
                    (query_argument, False),
                    (self.config, False),
                )
            ),
        )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        expression_result = SearchArguments.get_result(self.expression, expression_context, treat_str_as_field=True)
        query_expr = self.query if isinstance(self.query, (Expression, Term)) else SearchQuery(self.query)
        query_result = SearchArguments.get_result(query_expr, expression_context, treat_str_as_field=False)

        args = [expression_result.term, query_result.term]
        joins = ExpressionResult.dedup_joins(expression_result.joins, query_result.joins)

        if self.config is not None:
            config_result = SearchArguments.get_result(self.config, expression_context, treat_str_as_field=False)
            args = [config_result.term, *args]
            joins = ExpressionResult.dedup_joins(joins, config_result.joins)

        options = {key: value for key, value in self.options.items() if value is not None}
        if options:
            options_sql = ", ".join(
                f"{key}={SearchHeadline.format_option_value(value)}" for key, value in options.items()
            )
            args.append(ValueWrapper(options_sql))

        term = HareSqlFunction("TS_HEADLINE", *args)
        return ExpressionResult(term=term, joins=joins, output_field=TextField())

    @staticmethod
    def format_option_value(value: HeadlineOptionValue) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return str(value)
