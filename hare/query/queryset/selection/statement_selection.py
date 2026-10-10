from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.query.enums import RowShape
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.queryset.arguments.annotation_arguments import AnnotationArguments
from hare.query.queryset.arguments.values_arguments import ValuesArguments
from hare.query.queryset.combination.queryset_combination import QuerySetCombination
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions
from hare.query.queryset.options.query_options import QueryOptions
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.queryset.selection.values_selection import ValuesSelection
from hare.query.rewrites.query_rewrites import QueryRewrites
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from hare.query.statements.select.rows_query import RowsQuery
from hare.sql import Order

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.select.combined_query import CombinedQuery
    from hare.query.statements.select.values_query import ValuesQuery


SameQuerySet = TypeVar("SameQuerySet", bound="QuerySet[Any, Any]")


class StatementSelection:
    """The statement a queryset is run as: its rows as model instances, values or a set operation, and
    the queries made of its rows - a count, an existence test, an aggregate, an update, a subquery
    of their keys or of one field."""

    @staticmethod
    def get_select_query(queryset: QuerySet[Any, Any]) -> RowsQuery[Any]:
        """The query of this queryset's rows, made for the caller alone.

        Args:
            queryset: The queryset.

        Returns:
            The query of the combined rows, of the selected values, else of model instances.
        """
        if (
            queryset._options is not QueryOptions.DEFAULT
            and (rewritten := QueryRewrites.get_rewritten(queryset)) is not queryset
        ):
            return StatementSelection.get_select_query(rewritten)
        if queryset._combination is not None:
            return QuerySetCombination.get_combined_query(queryset)
        if queryset._selection is not None:
            return StatementSelection.get_values_query(queryset)
        return ModelRowsQuery(queryset)

    @staticmethod
    def get_rows_source(queryset: SameQuerySet) -> SameQuerySet | ValuesQuery | CombinedQuery:
        """What the queries derived from this queryset's rows (count, exists, aggregate, update,
        first/last) are made by.

        Args:
            queryset: The queryset.

        Returns:
            The query of the combined rows, of the selected values, else this queryset itself.
        """
        if (
            queryset._options is not QueryOptions.DEFAULT
            and (rewritten := QueryRewrites.get_rewritten(queryset)) is not queryset
        ):
            return StatementSelection.get_rows_source(rewritten)
        if queryset._combination is not None:
            return QuerySetCombination.get_combined_query(queryset)
        if queryset._selection is not None:
            return StatementSelection.get_values_query(queryset)
        return queryset

    @staticmethod
    def get_values_query(queryset: QuerySet[Any, Any]) -> ValuesQuery:
        """The query selecting the rows of this queryset as its ``.values()``/``.values_list()``
        call selects them.

        Args:
            queryset: The queryset.

        Returns:
            The query.
        """
        from hare.query.statements.select.values_query import ValuesQuery

        selection = cast("ValuesSelection", queryset._selection)
        annotations = queryset._annotations
        if selection.shape is RowShape.DICT:
            if selection.selects_every_field:
                fields_for_select = {name: name for name in ValuesArguments.get_every_selected_name(queryset)}
            else:
                fields_for_select = {name: name for name in selection.field_names}
                fields_for_select.update(selection.renamed_fields)
                if path_annotations := AnnotationArguments.get_path_annotations(queryset, fields_for_select.values()):
                    annotations = {**annotations, **path_annotations}
            values_query = ValuesQuery(
                queryset,
                selection,
                annotations,
                # A key renaming an annotation (values(key="annotation")) selects that annotation
                # under the key, even when the key was an .alias() before.
                queryset._alias_keys
                - set(fields_for_select.values())
                - {return_as for return_as, field in fields_for_select.items() if field in annotations},
                fields_for_select,
            )
        else:
            if path_annotations := AnnotationArguments.get_path_annotations(queryset, selection.field_names):
                annotations = {**annotations, **path_annotations}
            fields_for_select_list = list(selection.field_names) or ValuesArguments.get_every_selected_name(queryset)
            values_query = ValuesQuery(
                queryset,
                selection,
                annotations,
                # A named .alias() is selected like any annotation.
                queryset._alias_keys - set(fields_for_select_list),
                fields_for_select_list,
            )
        return values_query

    @staticmethod
    def get_primary_key_values_query(queryset: QuerySet[Any, Any]) -> ValuesQuery:
        """The primary key of each row this queryset returns, as a query to embed - sliced,
        ordered and ``.distinct()`` exactly like the rows themselves: a plain ``.distinct()``
        dedupes over the model columns plus the ordering columns, as the model query does.

        Args:
            queryset: The queryset.

        Returns:
            A ``values_list()`` query of the primary key column(s), flat for a single-column key.
        """
        if (
            queryset._options is not QueryOptions.DEFAULT
            and (rewritten := QueryRewrites.get_rewritten(queryset)) is not queryset
        ):
            return StatementSelection.get_primary_key_values_query(rewritten)
        rows_queryset = StatementSelection.get_values_rows_queryset(queryset)
        if distinct_annotation_names := StatementSelection.get_distinct_row_multiplying_annotation_names(queryset):
            # Ordered last by them, so the DISTINCT over the ordering columns keeps them apart too.
            rows_queryset._orderings = [
                *queryset._apply_default_ordering(queryset._orderings, queryset._annotations),
                *((annotation_name, Order.ASC) for annotation_name in distinct_annotation_names),
            ]
        primary_key_attribute_names = queryset.model._meta.primary_key_attribute_names
        values_queryset = rows_queryset.values_list(
            *primary_key_attribute_names, flat=len(primary_key_attribute_names) == 1
        )
        return StatementSelection.get_values_query(
            values_queryset._with_selection(
                replace(cast("ValuesSelection", values_queryset._selection), distinct_over_ordering_columns=True)
            )
        )

    @staticmethod
    def get_values_rows_queryset(queryset: SameQuerySet) -> SameQuerySet:
        """A clone selecting the same rows with nothing ``values_list()`` rejects - no
        ``.only()``/``.defer()``, ``select_related()``, ``prefetch_related()``, single-row narrowing
        or ``.group_by()``.

        Args:
            queryset: The queryset.

        Returns:
            The clone.
        """
        rows_queryset = queryset._clone()
        rows_queryset._group_bys = ()
        rows_queryset._prefetch_map = {}
        rows_queryset._prefetch_queries = {}
        rows_queryset._fields_for_select = ()
        rows_queryset._deferred_fields = ()
        rows_queryset._select_related = set()
        rows_queryset._explicitly_select_related = set()
        rows_queryset._single = False
        rows_queryset._raise_does_not_exist = False
        rows_queryset._selection = None
        return rows_queryset

    @staticmethod
    def get_rows_query_if_rows_repeat(queryset: QuerySet[Any, Any]) -> ValuesQuery | None:
        """The unsliced primary key query of the rows, when they repeat a primary key,
        ``.distinct(<fields>)`` picks one row per group or a call of the dialect's QuerySet method
        changes which rows are returned (``QuerySetExtension.changes_rows``) - ``count()``/``exists()``
        count them as returned.

        Args:
            queryset: The queryset.

        Returns:
            The query, or None when the rows are the plain matching rows.
        """
        orderings = queryset._apply_default_ordering(queryset._orderings, queryset._annotations)
        # A composite primary key has no single column for COUNT(DISTINCT ...) - a .distinct()
        # then counts its distinct rows as a derived table.
        distinct_over_composite_key = queryset._distinct and queryset.model._meta.has_composite_primary_key
        if (
            not queryset._distinct_on
            and not distinct_over_composite_key
            and not (
                orderings
                and any(
                    field_name not in queryset._annotations
                    and AggregatedMultiValuedPaths.get_multi_valued_paths(queryset.model, field_name)
                    for field_name, _order in orderings
                )
            )
            and not StatementSelection.get_distinct_row_multiplying_annotation_names(queryset)
            and not (queryset._extension_calls and QuerySetExtensions.changes_rows(queryset))
        ):
            return None
        unsliced_queryset = queryset._clone()
        unsliced_queryset._limit = None
        unsliced_queryset._offset = None
        return StatementSelection.get_primary_key_values_query(unsliced_queryset)

    @staticmethod
    def get_distinct_row_multiplying_annotation_names(queryset: QuerySet[Any, Any]) -> list[str]:
        """The selected annotations reading a to-many relation that a plain ``.distinct()`` keeps
        apart - the rows then repeat a primary key once per distinct value.

        Args:
            queryset: The queryset.

        Returns:
            The annotation names, empty when the queryset isn't a plain ``.distinct()`` one.
        """
        if not queryset._distinct or queryset._distinct_on or not queryset._annotations:
            return []
        return RowMultiplication.get_row_multiplying_annotation_names(queryset, selected_only=True)
