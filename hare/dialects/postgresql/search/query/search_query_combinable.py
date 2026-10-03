from __future__ import annotations

from typing import TYPE_CHECKING

from hare.query.expressions import Expression

if TYPE_CHECKING:
    from hare.dialects.postgresql.search.query.combined_search_query import CombinedSearchQuery


class SearchQueryCombinable(Expression, abstract=True):
    """Mixin giving `SearchQuery`/`CombinedSearchQuery` the ``|``/``&`` operators to combine
    tsqueries (Postgres ``||``/``&&``)."""

    def _combine(self, other: SearchQueryCombinable, operator: str, reversed: bool) -> CombinedSearchQuery:
        # Imported here: the modules import each other.
        from hare.dialects.postgresql.search.query.combined_search_query import CombinedSearchQuery

        if not isinstance(other, SearchQueryCombinable):
            raise TypeError(
                f"SearchQuery can only be combined with other SearchQuery instances, got {other.__class__.__name__}."
            )
        if reversed:
            return CombinedSearchQuery(other, operator, self)
        return CombinedSearchQuery(self, operator, other)

    def __or__(self, other: SearchQueryCombinable) -> CombinedSearchQuery:
        return self._combine(other, " || ", False)

    def __ror__(self, other: SearchQueryCombinable) -> CombinedSearchQuery:
        return self._combine(other, " || ", True)

    def __and__(self, other: SearchQueryCombinable) -> CombinedSearchQuery:
        return self._combine(other, " && ", False)

    def __rand__(self, other: SearchQueryCombinable) -> CombinedSearchQuery:
        return self._combine(other, " && ", True)
