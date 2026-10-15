from __future__ import annotations

from typing import TYPE_CHECKING

from hare.query.expressions import Expression
from hare.search.enums import SearchOperator

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.search.query.combined_search_query import CombinedSearchQuery


class SearchQueryCombinable(Expression, abstract=True):
    """Gives ``SearchQuery``/``CombinedSearchQuery`` the ``|``/``&`` operators combining queries."""

    def _combine(self, other: SearchQueryCombinable, operator: SearchOperator, reversed: bool) -> CombinedSearchQuery:
        # Local import: the modules import each other.
        from hare.search.query.combined_search_query import CombinedSearchQuery

        if not isinstance(other, SearchQueryCombinable):
            raise TypeError(
                f"SearchQuery can only be combined with other SearchQuery instances, got {other.__class__.__name__}."
            )
        if reversed:
            return CombinedSearchQuery(other, operator, self)
        return CombinedSearchQuery(self, operator, other)

    def __or__(self, other: SearchQueryCombinable) -> CombinedSearchQuery:
        return self._combine(other, SearchOperator.OR, False)

    def __ror__(self, other: SearchQueryCombinable) -> CombinedSearchQuery:
        return self._combine(other, SearchOperator.OR, True)

    def __and__(self, other: SearchQueryCombinable) -> CombinedSearchQuery:
        return self._combine(other, SearchOperator.AND, False)

    def __rand__(self, other: SearchQueryCombinable) -> CombinedSearchQuery:
        return self._combine(other, SearchOperator.AND, True)

    def __invert__(self) -> SearchQueryCombinable:
        raise NotImplementedError
