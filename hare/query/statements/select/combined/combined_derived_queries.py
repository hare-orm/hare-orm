from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import copy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import FieldError, QueryError
from hare.query.expressions import Ordering
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.rows.values_rows.combined_values_rows import CombinedValuesRows
from hare.query.statements.select.combined.combined_branches import CombinedBranches
from hare.query.statements.select.values.values_output import ValuesOutput
from hare.query.statements.select.values_query import ValuesQuery
from hare.sql import Order
from hare.sql.builder.queries.query_builder import QueryBuilder

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.queryset.combination.combination import Combination
    from hare.query.queryset.queryset import QuerySet
    from hare.query.relation_loading.prefetching.prefetch import Prefetch
    from hare.query.statements.select.combined_query import CombinedQuery


class CombinedDerivedQueries:
    """The querysets and queries made from a set operation: its rows ordered, its branches changed, its
    instances prefetched, its values, and its columns as a value embedded into another query."""

    @staticmethod
    def get_single_column_value_field(query: CombinedQuery) -> Field[Any] | None:
        """The value field of the one combined column of values, once built.

        Args:
            query: The set operation.

        Returns:
            The field, or None for several columns, an unknown field or model instances.
        """
        rows = query._rows
        if not isinstance(rows, CombinedValuesRows) or len(rows.column_value_fields) != 1:
            return None
        return rows.column_value_fields[0]

    @staticmethod
    def get_fields_values_query(
        query: CombinedQuery,
        field_names: Sequence[str],
        value_wrapper_references: RecordedValueReferences | None = None,
    ) -> QueryBuilder:
        """Columns of combined model instances, as a query to embed - a queryset combining
        querysets passed as an ``__in`` filter value, one column per compared key column.

        Args:
            query: The set operation.
            field_names: The fields to select, in order.
            value_wrapper_references: The list of the enclosing query the value references go into.

        Returns:
            The query selecting those columns from the combined rows.

        Raises:
            FieldError: The combined rows don't select one of the fields.
        """
        union = query._get_execution_query()
        union._plan_origin = query
        if value_wrapper_references is not None:
            union._make_subquery(value_wrapper_references=value_wrapper_references)
        else:
            union._make_subquery()
        rows_query = copy(union.query)
        columns = []
        for field_name in field_names:
            column_name = query.model._meta.fields_db_projection.get(field_name)
            if column_name not in union._rows.get_output_names():
                raise FieldError(f"The union doesn't select {field_name!r} - it can't be used as an __in filter value")
            columns.append(rows_query.field(column_name))
        return union._connection.query_class.from_(rows_query).select(*columns)

    @staticmethod
    def get_ordered_queryset(query: CombinedQuery, orderings: tuple[str | Ordering, ...]) -> QuerySet[Any, Any]:
        """The combined rows ordered by their columns (``"name"``, ``"-name"``, or an
        ``Ordering`` for explicit NULL placement) - values by their output names, model instances
        by their fields and annotations, ``pk`` being the primary key.

        Args:
            query: The set operation.
            orderings: The ordering names or ``Ordering`` expressions.

        Returns:
            The queryset.

        Raises:
            QueryError: A name isn't an output name of the values.
            QueryError: The rows are sliced - order them before slicing.
        """
        if query._limit is not None or query._offset:
            raise QueryError("Cannot reorder a query once a slice has been taken.")
        parsed_orderings: list[tuple[str, Order]] = []
        for ordering in orderings:
            field_name, order = query._get_ordering_string(ordering)
            if field_name == "pk" and not query._combines_values:
                parsed_orderings.extend(
                    (primary_key_attribute_name, order)
                    for primary_key_attribute_name in query.model._meta.primary_key_attribute_names
                )
                continue
            query._rows.check_ordering_name(field_name)
            parsed_orderings.append((field_name, order))
        queryset = query._source_queryset._clone()
        queryset._orderings = parsed_orderings
        return queryset

    @staticmethod
    def get_changed_branches_queryset(
        queryset: QuerySet[Any, Any], change_branch: Callable[[QuerySet[Any, Any]], QuerySet[Any, Any]]
    ) -> QuerySet[Any, Any]:
        """A copy of a queryset combining querysets with every queryset it combines changed.

        Args:
            queryset: The queryset.
            change_branch: Changes a queryset that combines none.

        Returns:
            The copy.
        """
        combination = cast("Combination", queryset._combination)
        changed_queryset = queryset._clone()
        changed_queryset._combination = replace(
            combination,
            branches=tuple(
                CombinedDerivedQueries.get_changed_branches_queryset(branch, change_branch)
                if branch._combination is not None
                else change_branch(branch)
                for branch in combination.branches
            ),
        )
        return changed_queryset

    @staticmethod
    def get_prefetching_queryset(query: CombinedQuery, relations: tuple[str | Prefetch, ...]) -> QuerySet[Any, Any]:
        """The combined model instances with relations prefetched on them once they are read -
        batched over the instances, grouped by their model.

        Args:
            query: The set operation.
            relations: Relation names or ``Prefetch(...)`` instances.

        Returns:
            The queryset.

        Raises:
            QueryError: The rows are values - there is nothing to attach a relation to.
        """
        if query._combines_values:
            raise QueryError(
                ".values()/.values_list() cannot be used with prefetch_related() - the result is plain "
                "tuples/dicts, not model instances, so there's nothing to attach a prefetched relation to."
            )
        queryset = query._source_queryset._clone()
        queryset._combination = replace(
            cast("Combination", queryset._combination),
            prefetched_relations=query._prefetched_relations + tuple(relations),
        )
        return queryset

    @staticmethod
    def get_values_queryset(
        query: CombinedQuery, select_values: Callable[[QuerySet[Any, Any]], QuerySet[Any, Any]]
    ) -> QuerySet[Any, Any]:
        """The same set operation over ``.values()``/``.values_list()`` of every branch, like
        Django. The ordering and slice of the combined rows carry over.

        Args:
            query: The set operation.
            select_values: Applies the ``.values()``/``.values_list()`` call to a branch.

        Returns:
            The queryset combining the values.

        Raises:
            QueryError: The rows are values already, the combined model instances prefetch
                relations, or are ordered by a field the values don't select.
        """
        # Local import: the combined query imports this module.
        from hare.query.statements.select.combined_query import CombinedQuery

        if query._combines_values:
            raise QueryError(
                ".values()/.values_list() can't be used on a set operation of .values()/.values_list() "
                "querysets - call it on each queryset before combining them."
            )
        if query._prefetched_relations:
            raise QueryError(
                ".values()/.values_list() cannot be used with prefetch_related() - the result is plain "
                "tuples/dicts, not model instances, so there's nothing to attach a prefetched relation to."
            )
        queryset = CombinedDerivedQueries.get_changed_branches_queryset(query._source_queryset, select_values)
        queryset._combination = replace(cast("Combination", queryset._combination), prefetched_relations=())
        first_leaf = cast("ValuesQuery", CombinedBranches.get_first_leaf(CombinedQuery(queryset)))
        output_name_by_selected_name = dict(
            zip(
                ValuesOutput.get_output_field_names(first_leaf),
                ValuesOutput.get_output_names_for_set_operation(first_leaf),
                strict=True,
            )
        )
        orderings: list[tuple[str, Order]] = []
        for field_name, order in query._orderings:
            output_name = output_name_by_selected_name.get(field_name)
            if output_name is None:
                raise QueryError(
                    f"The union is ordered by {field_name!r}, which .values()/.values_list() doesn't select - "
                    "select it too."
                )
            orderings.append((output_name, order))
        queryset._orderings = orderings
        return queryset
