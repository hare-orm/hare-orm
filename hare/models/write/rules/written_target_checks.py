from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError

if TYPE_CHECKING:
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.queryset.queryset import QuerySet


class WrittenTargetChecks:
    """What a write refuses of the queryset it takes its rows from, whichever write it is: a combined
    queryset (a ``union()``), a sample of the table, a queryset selecting values but for an update, and - once
    the connection is known - a called method that refuses writes."""

    @staticmethod
    def raise_if_refused(queryset: QuerySet[Any, Any], method_name: str, *, allows_values: bool = False) -> None:
        """Rejects a queryset a write can't take its rows from - when the write is called.

        Args:
            queryset: The queryset written.
            method_name: The writing method, without parentheses - named in the messages.
            allows_values: Whether a queryset selecting values is taken - an update writes the model rows
                the values come from, like Django.

        Raises:
            QueryError: The queryset is combined, reads a sample, or selects values where that isn't
                allowed.
        """
        # The refusals are imported only to be raised - every write runs these checks. Local imports: the
        # queryset package imports the write package.
        if queryset._combination is not None:
            from hare.query.queryset.combination.queryset_combination import QuerySetCombination

            QuerySetCombination.raise_if_combined(queryset, method_name)
        if queryset._table_sample is not None:
            raise QueryError(
                f"{method_name}() can't be used on a sample() - write the sampled rows by their keys: "
                f"Model.objects.filter(pk__in=queryset.sample(...)).{method_name}(...)"
            )
        if not allows_values and queryset._selection is not None:
            from hare.query.queryset.arguments.values_arguments import ValuesArguments

            ValuesArguments.raise_if_values_selected(queryset, method_name)

    @staticmethod
    def raise_if_refused_calls(query: QuerySpecification[Any]) -> None:
        """Rejects a write whose queryset called a method that refuses writes - when the write runs, its
        connection's dialect known.

        Args:
            query: The write's query, on its connection.

        Raises:
            QueryError: A called method refuses writes.
            UnSupportedError: The connection's dialect has no implementation of a called method.
        """
        # Local import: the queryset package imports the write package.
        from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions

        if query._extension_calls:
            QuerySetExtensions.raise_if_refused_in_write(query)
