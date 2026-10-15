from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.query.rewrites.distinct_on_emulation import DistinctOnEmulation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.queryset import QuerySet

RewrittenQuerySet = TypeVar("RewrittenQuerySet", bound="QuerySet[Any, Any]")


class QueryRewrites:
    """The rewrites of a queryset's specification for the connection it runs on, made before any
    statement is made of it - in one place, for every statement a queryset makes: its rows, its
    count, a subquery of its key, a branch of a set operation."""

    @staticmethod
    def get_rewritten(queryset: RewrittenQuerySet) -> RewrittenQuerySet:
        """The queryset as the connection runs it - ``DISTINCT ON`` emulated where the database has
        none.

        Callers skip the call for a queryset without settings (``QueryOptions.DEFAULT``): none of the
        rewrites applies to it.

        Args:
            queryset: The queryset.

        Returns:
            The rewritten queryset, or the queryset itself when nothing is rewritten.
        """
        if queryset._distinct_on:
            return cast("RewrittenQuerySet", DistinctOnEmulation.get_emulated(queryset))
        return queryset
