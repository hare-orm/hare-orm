from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import QueryError
from hare.query.queryset.combination.combination import Combination
from hare.sql.enums import SetOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.select.combined_query import CombinedQuery


SameQuerySet = TypeVar("SameQuerySet", bound="QuerySet[Any, Any]")


class QuerySetCombination:
    """The set operations of querysets - union(), intersection() and difference() combining their rows,
    and the methods a combined queryset refuses."""

    @staticmethod
    def combine(queryset: SameQuerySet, other_querysets: tuple[Any, ...], set_operation: SetOperation) -> SameQuerySet:
        """The queryset of this queryset's rows combined with other querysets' rows.

        Args:
            queryset: The queryset.
            other_querysets: The other querysets.
            set_operation: The operation combining each of them with the whole result so far.

        Returns:
            The queryset.

        Raises:
            QueryError: A branch isn't a queryset, or model instances are combined with
                ``.values()``/``.values_list()`` rows.
        """
        # Local import: the queryset module imports this one.
        from hare.query.queryset.queryset import QuerySet

        selects_values = QuerySetCombination.selects_values(queryset)
        for other_queryset in other_querysets:
            if not isinstance(other_queryset, QuerySet):
                raise QueryError(
                    f"union()/intersection()/difference() combine querysets, got {type(other_queryset).__name__}"
                )
            if QuerySetCombination.selects_values(other_queryset) != selects_values:
                raise QueryError(
                    "Cannot combine model instances with .values()/.values_list() rows - call .values()/"
                    ".values_list() on every branch (the rows then take the first branch's shape)."
                )
        combined_rows_are_one_branch = selects_values and bool(
            queryset._orderings or queryset._limit is not None or queryset._offset or queryset._is_none
        )
        combination = queryset._combination
        if combination is not None and not combined_rows_are_one_branch:
            clone = queryset._clone()
            clone._combination = combination.with_branches(other_querysets, set_operation)
            return clone
        # A queryset of the combined rows on this queryset's connection - this queryset, the
        # combined rows so far when they are ordered, sliced or empty, is its first branch.
        combined_queryset = type(queryset)(queryset.model)
        combined_queryset._apply_connection(queryset._connection)
        combined_queryset._connection_explicitly_chosen = queryset._connection_explicitly_chosen
        combined_queryset._router_fallback_connection = queryset._router_fallback_connection
        combined_queryset._instance_connection_alias = queryset._instance_connection_alias
        combined_queryset._visibility = queryset._visibility
        combined_queryset._combination = Combination.of((queryset, *other_querysets), set_operation)
        return combined_queryset

    @staticmethod
    def get_combined_query(queryset: QuerySet[Any, Any]) -> CombinedQuery:
        """The query building the rows this queryset combines.

        Args:
            queryset: The queryset.
        """
        from hare.query.statements.select.combined_query import CombinedQuery

        return CombinedQuery(queryset)

    @staticmethod
    def selects_values(queryset: QuerySet[Any, Any]) -> bool:
        """Whether the rows are the values ``.values()``/``.values_list()`` select - this queryset's,
        or the first of the querysets it combines.

        Args:
            queryset: The queryset.
        """
        if queryset._combination is not None:
            return QuerySetCombination.selects_values(queryset._combination.branches[0])
        return queryset._selection is not None

    @staticmethod
    def raise_if_combined(queryset: QuerySet[Any, Any], method_name: str) -> None:
        """Rejects a method changing which rows a queryset matches on a queryset combining
        querysets, like Django.

        Args:
            queryset: The queryset.
            method_name: The method.

        Raises:
            QueryError: The queryset combines querysets.
        """
        if queryset._combination is not None:
            raise QueryError(
                f"{method_name}() can't be used on a union()/intersection()/difference() - call it on each "
                "queryset before combining them."
            )
