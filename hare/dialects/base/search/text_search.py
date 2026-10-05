from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
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
