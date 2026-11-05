from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import FieldError
from hare.query.generic_foreign_keys.generic_foreign_key_types import GenericForeignKeyTypes
from hare.query.queryset.arguments.annotation_arguments import AnnotationArguments
from hare.query.queryset.arguments.ordering_arguments import OrderingArguments
from hare.query.queryset.combination.queryset_combination import QuerySetCombination
from hare.query.queryset.constants import LATEST_WITHOUT_FIELDS_MESSAGE
from hare.query.queryset.selection.statement_selection import StatementSelection
from hare.query.queryset.single_rows.query_set_single import QuerySetSingle

if TYPE_CHECKING:
    from hare.query.expressions.expression import Expression
    from hare.query.expressions.ordering import Ordering
    from hare.query.queryset.queryset import QuerySet


SingleRowQuerySet = TypeVar("SingleRowQuerySet", bound="QuerySet[Any, Any]")


class SingleRow:
    """The queryset of one row - get(), first(), last(), latest(), earliest() - its rows taken as a
    subquery where they repeat or are distinct on fields."""

    @staticmethod
    def as_single(queryset: QuerySet[Any, Any]) -> QuerySetSingle[Any]:
        """Makes a queryset return its first row as one object - a slice of it keeps its offset.

        Args:
            queryset: The queryset, changed in place.

        Returns:
            The queryset as a query of one row.
        """
        if not queryset._single and (queryset._limit is not None or queryset._offset):
            queryset._is_single_row_of_slice = True
        queryset._single = True
        queryset._limit = 1 if queryset._limit is None else min(queryset._limit, 1)
        return queryset  # type: ignore[return-value]

    @staticmethod
    def with_rows_as_subquery(queryset: SingleRowQuerySet) -> SingleRowQuerySet:
        """A clone restricted to this queryset's rows by ``pk IN (subquery)``, with its own slice and
        ``.distinct(<fields>)`` cleared - a later reordering or filter stays within those rows.

        Args:
            queryset: The queryset.

        Returns:
            A plain clone when this queryset is neither sliced nor ``.distinct(<fields>)``. The rows of
            an unordered slice are the first ones by primary key.
        """
        clone = queryset._clone()
        if queryset._limit is None and not queryset._offset and not queryset._distinct_on:
            return clone
        queryset.model._meta.raise_if_no_primary_key("narrowing a sliced or distinct(*fields) queryset's rows")
        primary_key_attribute = queryset.model._meta.primary_key_attribute
        # A composite key compares its columns as one row value: (a, b) IN (SELECT a, b ...).
        pk_filter_key = "pk__in" if isinstance(primary_key_attribute, tuple) else f"{primary_key_attribute}__in"
        clone._limit = None
        clone._offset = None
        clone._distinct = False
        clone._distinct_on = []
        # An unordered slice holds the first rows by primary key - the rows first() reads.
        rows_source = OrderingArguments.with_primary_key_ordering_if_unordered_slice(queryset)
        clone._append_filters(False, (), {pk_filter_key: StatementSelection.get_primary_key_values_query(rows_source)})
        # A row picked from a slice can't be filtered or reordered any further, as for first().
        clone._is_single_row_of_slice = queryset._is_single_row_of_slice or (
            queryset._limit is not None or bool(queryset._offset)
        )
        return clone

    @staticmethod
    def with_distinct_on_rows_as_subquery(queryset: SingleRowQuerySet) -> SingleRowQuerySet:
        """A clone restricted to the rows ``.distinct(<fields>)`` picks via a ``pk IN (subquery)``
        filter of the unsliced queryset, keeping its own slice - so a keyset boundary applies to
        those rows, not to the rows the distinct picks from.

        Args:
            queryset: The queryset.

        Returns:
            A plain clone when this queryset isn't ``.distinct(<fields>)``.
        """
        if not queryset._distinct_on:
            return queryset._clone()
        unsliced_queryset = queryset._clone()
        unsliced_queryset._limit = None
        unsliced_queryset._offset = None
        narrowed_queryset = SingleRow.with_rows_as_subquery(unsliced_queryset)
        narrowed_queryset._limit = queryset._limit
        narrowed_queryset._offset = queryset._offset
        return narrowed_queryset

    @staticmethod
    def raise_if_sliced_values_rows_differ(queryset: QuerySet[Any, Any], method_name: str) -> None:
        """Rejects re-selecting within a slice of a queryset selecting values whose rows aren't one
        per model row - grouped, deduplicated or multiplied by a to-many relation.

        Args:
            queryset: The queryset.
            method_name: The calling method, for the message.

        Raises:
            QueryError: Such a sliced queryset.
        """
        if queryset._selection is not None and (queryset._limit is not None or queryset._offset):
            StatementSelection.get_values_query(queryset)._raise_if_sliced_rows_differ_from_source(method_name)

    @staticmethod
    def get_extreme(
        queryset: QuerySet[Any, Any], orderings: Sequence[str | Expression | Ordering], *, reverse: bool
    ) -> QuerySetSingle[Any]:
        """Select the earliest or latest row using the shared ordering and slice rules.

        Args:
            queryset: The declaration to select from, left unchanged.
            orderings: Explicit orderings, or empty to use Meta.get_latest_by.
            reverse: Reverse the orderings for latest(); explicit NULL placement reverses too.

        Returns:
            The single-row query, with expression and path orderings declared as aliases.
        """
        method_name = "latest" if reverse else "earliest"
        QuerySetCombination.raise_if_combined(queryset, method_name)
        SingleRow.raise_if_sliced_values_rows_differ(queryset, method_name)
        orderings = orderings or queryset.model._meta.get_latest_by
        if not orderings:
            raise FieldError(LATEST_WITHOUT_FIELDS_MESSAGE.format(method=method_name))
        expression_aliases, orderings = OrderingArguments.get_expression_aliases(orderings)
        if expression_aliases:
            return getattr(queryset._alias(expression_aliases), method_name)(*orderings)
        if type_aliases := GenericForeignKeyTypes.get_pending_type_aliases(queryset, tuple(orderings)):
            return getattr(queryset.alias(**type_aliases), method_name)(*orderings)
        # A path into a JSON field, a date or an array orders by an alias of its own, as in order_by().
        if path_annotations := AnnotationArguments.get_path_annotations(
            queryset, (queryset._get_ordering_name(ordering)[0] for ordering in orderings)
        ):
            return getattr(queryset._alias(path_annotations), method_name)(*orderings)
        OrderingArguments.forbid_reordering_after_cursor(queryset, method_name)
        result = SingleRow.with_rows_as_subquery(queryset)
        result._orderings = OrderingArguments.parse_orderings(
            queryset, orderings, reverse=reverse, nulls_last_by_default=True
        )
        return SingleRow.as_single(result)
