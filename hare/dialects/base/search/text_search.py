from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.search.enums import TextSearchOperation
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.query.expressions import ExpressionContext, ExpressionResult
    from hare.search import (
        CombinedSearchQuery,
        CombinedSearchVector,
        SearchHeadline,
        SearchQuery,
        SearchRank,
        SearchVector,
    )


class TextSearch:
    """How a dialect runs ``hare.search``'s full-text search - the SQL of its queries, vectors,
    ranks and headlines. A dialect without full-text search keeps this one: each expression raises
    ``UnSupportedError`` before any SQL is sent.

    Args:
        dialect: The dialect.
    """

    def __init__(self, dialect: Dialect) -> None:
        self.dialect = dialect

    def require_operation(
        self, expression_context: ExpressionContext, operation: TextSearchOperation, name: str
    ) -> None:
        """Check the effective connection features before building any argument."""
        connection = expression_context.connection
        features = connection.features if connection is not None else expression_context.dialect.features
        if operation not in features.text_search_operations:
            raise UnSupportedError(
                f"{name} can't run on the {self.dialect} dialect: it needs text_search_operations.{operation.value}"
            )

    @staticmethod
    def check_configuration(expression_context: ExpressionContext, config: object, name: str) -> None:
        """Explicit configurations require configuration support, independently of other options."""
        from hare.search.constants import TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE
        from hare.search.search_features import SearchFeatures

        if config is not None:
            SearchFeatures.raise_if_unsupported(expression_context, name, TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE)

    def check_query(self, query: SearchQuery, expression_context: ExpressionContext) -> None:
        """Check query configuration and raw query expressions independently."""
        from hare.search.query.declarations import RawSearchQueryText

        self.check_configuration(expression_context, query.config, "SearchQuery(config=...)")
        if isinstance(query.value, RawSearchQueryText):
            self.require_operation(
                expression_context, TextSearchOperation.RAW_QUERY_EXPRESSION, "SearchQuery(raw expression)"
            )

    def check_vector(self, vector: SearchVector, expression_context: ExpressionContext) -> None:
        """Check vector construction, configuration and optional label weight."""
        self.require_operation(expression_context, TextSearchOperation.VECTOR, "SearchVector")
        self.check_configuration(expression_context, vector.config, "SearchVector(config=...)")
        if vector.weight is not None:
            self.require_operation(expression_context, TextSearchOperation.VECTOR_WEIGHT, "SearchVector(weight=...)")

    def check_rank(self, rank: SearchRank, expression_context: ExpressionContext) -> None:
        """Check every optional ranking operation against the selected connection."""
        if rank.weights is not None:
            operation = (
                TextSearchOperation.RANK_FIELD_WEIGHTS
                if isinstance(rank.weights, dict)
                else TextSearchOperation.RANK_LABEL_WEIGHTS
            )
            self.require_operation(expression_context, operation, "SearchRank(weights=...)")
        if rank.normalization is not None:
            self.require_operation(
                expression_context, TextSearchOperation.RANK_NORMALIZATION, "SearchRank(normalization=...)"
            )
        if rank.cover_density:
            self.require_operation(
                expression_context, TextSearchOperation.RANK_COVER_DENSITY, "SearchRank(cover_density=True)"
            )

    def check_headline(self, headline: SearchHeadline, expression_context: ExpressionContext) -> None:
        """Check each supplied headline option; an unset option imposes no requirement."""
        self.check_configuration(expression_context, headline.config, "SearchHeadline(config=...)")
        for name in headline.get_options():
            self.require_operation(
                expression_context, TextSearchOperation(f"headline_{name}"), f"SearchHeadline({name}=...)"
            )

    def get_unsupported_error(self, name: str) -> UnSupportedError:
        """The error of a search expression the dialect doesn't run.

        Args:
            name: The expression.

        Returns:
            The error, to raise.
        """
        return UnSupportedError(f"{name} can't run on the {self.dialect} dialect: it has no full-text search")

    def get_query_result(self, query: SearchQuery, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves a search query.

        Args:
            query: The query.
            expression_context: The context it is resolved in.

        Returns:
            The query's term.

        Raises:
            UnSupportedError: The dialect has no full-text search.
        """
        raise self.get_unsupported_error("SearchQuery")

    def get_combined_query_result(
        self, query: CombinedSearchQuery, expression_context: ExpressionContext
    ) -> ExpressionResult:
        """Resolves two search queries combined.

        Args:
            query: The combination.
            expression_context: The context it is resolved in.

        Returns:
            The combination's term.

        Raises:
            UnSupportedError: The dialect has no full-text search.
        """
        raise self.get_unsupported_error("SearchQuery & SearchQuery")

    def get_vector_result(self, vector: SearchVector, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves a search vector as a value of its own.

        Args:
            vector: The vector.
            expression_context: The context it is resolved in.

        Returns:
            The vector's term.

        Raises:
            UnSupportedError: The dialect has no search vectors.
        """
        raise self.get_unsupported_error("SearchVector")

    def get_combined_vector_result(
        self, vector: CombinedSearchVector, expression_context: ExpressionContext
    ) -> ExpressionResult:
        """Resolves two search vectors concatenated.

        Args:
            vector: The concatenation.
            expression_context: The context it is resolved in.

        Returns:
            The concatenation's term.

        Raises:
            UnSupportedError: The dialect has no search vectors.
        """
        raise self.get_unsupported_error("SearchVector + SearchVector")

    def get_rank_result(self, rank: SearchRank, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves a search rank.

        Args:
            rank: The rank.
            expression_context: The context it is resolved in.

        Returns:
            The rank's term.

        Raises:
            UnSupportedError: The dialect has no full-text search.
        """
        raise self.get_unsupported_error("SearchRank")

    def get_headline_result(self, headline: SearchHeadline, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves a search headline.

        Args:
            headline: The headline.
            expression_context: The context it is resolved in.

        Returns:
            The headline's term.

        Raises:
            UnSupportedError: The dialect has no full-text search.
        """
        raise self.get_unsupported_error("SearchHeadline")
