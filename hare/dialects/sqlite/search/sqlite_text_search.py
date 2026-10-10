from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.dialects.base.search.text_search import TextSearch
from hare.dialects.sqlite.indexes.full_text_index import FullTextIndex
from hare.dialects.sqlite.search.constants import (
    SQLITE_FULL_TEXT_DEFAULT_FRAGMENT_DELIMITER,
    SQLITE_FULL_TEXT_DEFAULT_START_SELECTION,
    SQLITE_FULL_TEXT_DEFAULT_STOP_SELECTION,
    SQLITE_FULL_TEXT_NOT_OPERATOR,
    SQLITE_FULL_TEXT_QUERY_OPERATORS,
    SQLITE_FULL_TEXT_SNIPPET_MAX_WORDS,
)
from hare.dialects.sqlite.search.terms.full_text_headline import FullTextHeadline
from hare.dialects.sqlite.search.terms.full_text_match import FullTextMatch
from hare.dialects.sqlite.search.terms.full_text_query import FullTextQuery
from hare.dialects.sqlite.search.terms.full_text_query_combination import FullTextQueryCombination
from hare.dialects.sqlite.search.terms.full_text_rank import FullTextRank
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields import FloatField, TextField
from hare.query.expressions import ExpressionContext, ExpressionResult
from hare.search.constants import FULL_TEXT_INDEX_REQUIRED_FEATURE
from hare.search.enums import SearchOperator, SearchType
from hare.search.query.search_query import SearchQuery
from hare.search.search_arguments import SearchArguments
from hare.search.search_features import SearchFeatures
from hare.search.vector.search_vector import SearchVector
from hare.sql.terms.field import Field as HareSqlField
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.models import Model
    from hare.search import CombinedSearchQuery, SearchHeadline, SearchQueryCombinable, SearchRank


class SqliteTextSearch(TextSearch):
    """SQLite's full-text search - FTS5 through the model's ``FullTextIndex``: ``MATCH`` for a
    search, ``bm25()`` for a rank, ``highlight()``/``snippet()`` for a headline. A query restricted
    to some fields of the index carries an FTS5 column filter."""

    @staticmethod
    def get_index(model: type[Model], field_names: Iterable[str], searched_name: str) -> FullTextIndex:
        """The model's full-text index over the fields.

        Args:
            model: The model.
            field_names: The searched fields.
            searched_name: What searches them, for the message.

        Returns:
            The index.

        Raises:
            UnSupportedError: No ``FullTextIndex`` of the model covers the fields.
        """
        field_names = tuple(field_names)
        index = FullTextIndex.get_covering(model, field_names)
        if index is None:
            raise UnSupportedError(
                f"{model.__name__}: {searched_name} on SQLite needs a FullTextIndex over {list(field_names)!r} in "
                "Meta.indexes - SQLite searches text through an FTS5 index"
            )
        return index

    @staticmethod
    def get_column_filter(column_names: Any) -> str:
        """The FTS5 column filter of some columns of a full-text index.

        Args:
            column_names: The columns.

        Returns:
            ``{"title" "body"}``.
        """
        return "{" + " ".join('"' + column.replace('"', '""') + '"' for column in column_names) + "}"

    @staticmethod
    def is_negated(query: SearchQueryCombinable) -> bool:
        """Whether a search query is negated.

        Args:
            query: The query.

        Returns:
            True for ``~query``.
        """
        return query.invert if isinstance(query, SearchQuery) else query.negated  # type: ignore[attr-defined]

    @staticmethod
    def get_query_term(term: Term, searched_name: str) -> FullTextQuery | FullTextQueryCombination:
        """A resolved search query, as an FTS5 query.

        Args:
            term: The resolved query.
            searched_name: What searches with it, for the message.

        Returns:
            The query.

        Raises:
            UnSupportedError: The query is a SQL term of its own, not a ``SearchQuery``.
        """
        if not isinstance(term, (FullTextQuery, FullTextQueryCombination)):
            raise UnSupportedError(
                f"{searched_name} on SQLite searches with a SearchQuery or search text, not {term!r}"
            )
        return term

    def get_query_result(self, query: SearchQuery, expression_context: ExpressionContext) -> ExpressionResult:
        if query.invert:
            raise UnSupportedError(
                "~SearchQuery can't run on SQLite on its own: FTS5 negates a query only as the right side of "
                "&, as in SearchQuery(...) & ~SearchQuery(...)"
            )
        text_result = SearchArguments.get_result(query, "value", expression_context, treat_str_as_field=False)
        return ExpressionResult(term=FullTextQuery(text_result.term, query.search_type), joins=text_result.joins)

    def get_combined_query_result(
        self, query: CombinedSearchQuery, expression_context: ExpressionContext
    ) -> ExpressionResult:
        right_negated = self.is_negated(query.right)
        if (
            query.negated
            or self.is_negated(query.left)
            or (right_negated and query.operator is not SearchOperator.AND)
        ):
            raise UnSupportedError(
                "A negated SearchQuery can't run on SQLite here: FTS5 negates a query only as the right side of "
                "&, as in SearchQuery(...) & ~SearchQuery(...)"
            )
        left = query.left.get_result(expression_context)
        right_query = query.right
        if right_negated:
            right_query = ~right_query
            # Made again for each build - its values are the negated query's.
            right_query._plan_origin = query.right
        right = right_query.get_result(expression_context)
        operator = SQLITE_FULL_TEXT_NOT_OPERATOR if right_negated else SQLITE_FULL_TEXT_QUERY_OPERATORS[query.operator]
        return ExpressionResult(
            term=FullTextQueryCombination(left.term, operator, right.term),
            joins=ExpressionResult.dedup_joins(left.joins, right.joins),
        )

    @staticmethod
    def get_ranked_field_names(rank: SearchRank) -> tuple[str, ...]:
        """The fields a rank ranks.

        Args:
            rank: The rank.

        Returns:
            Their names.

        Raises:
            UnSupportedError: The rank's vector is an expression, not fields.
        """
        if isinstance(rank.vector, str):
            return (rank.vector,)
        if isinstance(rank.vector, tuple):
            return rank.vector
        field_names = rank.vector.get_field_names() if isinstance(rank.vector, SearchVector) else None
        if field_names is None:
            raise UnSupportedError(
                "SearchRank on SQLite ranks fields of the model's FullTextIndex - pass field names, not "
                f"{rank.vector!r}"
            )
        return field_names

    def get_rank_result(self, rank: SearchRank, expression_context: ExpressionContext) -> ExpressionResult:
        SearchFeatures.raise_if_unsupported(expression_context, "SearchRank", FULL_TEXT_INDEX_REQUIRED_FEATURE)
        field_names = self.get_ranked_field_names(rank)
        model = expression_context.model
        index = self.get_index(model, field_names, "SearchRank")
        field_weights: dict[str, float] = rank.weights if isinstance(rank.weights, dict) else {}
        unknown_weight_names = set(field_weights) - set(index.fields)
        if unknown_weight_names:
            raise ConfigurationError(
                f"SearchRank weights name {sorted(unknown_weight_names)!r} - not fields of its FullTextIndex "
                f"{list(index.fields)!r}"
            )
        columns = model._meta.get_column_names(field_names)
        index_columns = model._meta.get_column_names(index.fields)
        column_filter = "" if set(columns) == set(index_columns) else self.get_column_filter(columns)
        query_result = SearchArguments.get_result(rank, "query_argument", expression_context, treat_str_as_field=False)
        query = self.get_query_term(query_result.term, "SearchRank")
        weights = (
            tuple(float(field_weights.get(field_name, 1.0)) for field_name in index.fields) if field_weights else ()
        )
        row_key = HareSqlField(index.get_row_key_column(model), table=expression_context.table)
        term = FullTextRank(row_key, index.get_table_name(model), query.with_column_filter(column_filter), weights)
        return ExpressionResult(term=term, joins=query_result.joins, output_field=FloatField())

    def get_headline_result(self, headline: SearchHeadline, expression_context: ExpressionContext) -> ExpressionResult:
        SearchFeatures.raise_if_unsupported(expression_context, "SearchHeadline", FULL_TEXT_INDEX_REQUIRED_FEATURE)
        if not isinstance(headline.expression, str):
            raise UnSupportedError(
                "SearchHeadline on SQLite marks a field of the model's FullTextIndex - pass a field name, not "
                f"{headline.expression!r}"
            )
        if headline.max_words is not None and headline.max_words > SQLITE_FULL_TEXT_SNIPPET_MAX_WORDS:
            raise ConfigurationError(
                f"SearchHeadline max_words must be an int from 1 to {SQLITE_FULL_TEXT_SNIPPET_MAX_WORDS} on SQLite "
                f"- FTS5's snippet() returns no more words, got {headline.max_words!r}"
            )
        model = expression_context.model
        field_name = headline.expression
        index = self.get_index(model, (field_name,), "SearchHeadline")
        query_result = SearchArguments.get_result(
            headline, "query_argument", expression_context, treat_str_as_field=False
        )
        query = self.get_query_term(query_result.term, "SearchHeadline")
        column = HareSqlField(model._meta.get_column_names([field_name])[0], table=expression_context.table)
        row_key = HareSqlField(index.get_row_key_column(model), table=expression_context.table)
        term = FullTextHeadline(
            row_key,
            index.get_table_name(model),
            query.with_column_filter(""),
            column,
            index.fields.index(field_name),
            headline.start_sel if headline.start_sel is not None else SQLITE_FULL_TEXT_DEFAULT_START_SELECTION,
            headline.stop_sel if headline.stop_sel is not None else SQLITE_FULL_TEXT_DEFAULT_STOP_SELECTION,
            headline.fragment_delimiter
            if headline.fragment_delimiter is not None
            else SQLITE_FULL_TEXT_DEFAULT_FRAGMENT_DELIMITER,
            headline.max_words,
        )
        return ExpressionResult(term=term, joins=query_result.joins, output_field=TextField())

    def get_search_criterion(self, term: Term, value: Any, searched_field: Field[Any] | None = None) -> FullTextMatch:
        """``field__search=value`` - the rows the term's full-text index matches the search text
        (``SearchType.PLAIN``) or a ``SearchQuery`` for, in the term's column.

        Args:
            term: The term's column.
            value: The search text or a resolved ``SearchQuery``.
            searched_field: The searched term.

        Returns:
            The criterion.

        Raises:
            UnSupportedError: The value isn't a model term's, or no ``FullTextIndex`` covers it.
        """
        if searched_field is None or not isinstance(term, HareSqlField):
            raise UnSupportedError(
                "__search on SQLite searches a model's field through its FullTextIndex - not an annotation"
            )
        model = searched_field.model
        field_name = searched_field.model_field_name
        index = self.get_index(model, (field_name,), f"{field_name}__search")
        query = (
            value
            if isinstance(value, (FullTextQuery, FullTextQueryCombination))
            else FullTextQuery(value, SearchType.PLAIN)
        )
        column_filter = self.get_column_filter(model._meta.get_column_names([field_name]))
        row_key = HareSqlField(index.get_row_key_column(model), table=term.table)
        return FullTextMatch(row_key, index.get_table_name(model), query.with_column_filter(column_filter))
