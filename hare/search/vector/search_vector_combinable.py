from __future__ import annotations

from typing import TYPE_CHECKING

from hare.query.expressions import Expression

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.search.vector.combined_search_vector import CombinedSearchVector


class SearchVectorCombinable(Expression, abstract=True):
    """Gives ``SearchVector``/``CombinedSearchVector`` the ``+`` operator concatenating vectors."""

    def _combine(self, other: SearchVectorCombinable, reversed: bool) -> CombinedSearchVector:
        # Local import: the modules import each other.
        from hare.search.vector.combined_search_vector import CombinedSearchVector

        if not isinstance(other, SearchVectorCombinable):
            raise TypeError(
                f"SearchVector can only be combined with other SearchVector instances, got {other.__class__.__name__}."
            )
        if reversed:
            return CombinedSearchVector(other, self)
        return CombinedSearchVector(self, other)

    def __add__(self, other: SearchVectorCombinable) -> CombinedSearchVector:
        return self._combine(other, False)

    def __radd__(self, other: SearchVectorCombinable) -> CombinedSearchVector:
        return self._combine(other, True)
