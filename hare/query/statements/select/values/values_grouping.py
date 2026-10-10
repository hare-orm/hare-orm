from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.expressions import Expression
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.select.values.values_output import ValuesOutput
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.select.values_query import ValuesQuery


class ValuesGrouping:
    """Whether the rows of values() are groups and by what: an explicit group_by(), or the selected
    columns of a query aggregating after values() - the annotations that are neither aggregates nor
    window functions included."""

    @staticmethod
    def get_grouped_source_queryset(query: ValuesQuery) -> QuerySet[Any]:
        """The source queryset, explicitly grouped by the selected fields an aggregate implicitly
        groups this query by.

        Args:
            query: The values query.

        Returns:
            The queryset.
        """
        source_queryset = query._source_queryset
        if query._group_bys or not ValuesGrouping.is_grouped(query):
            return source_queryset
        group_key_names = ValuesGrouping.get_group_key_names(query)
        return source_queryset.group_by(*group_key_names) if group_key_names else source_queryset

    @staticmethod
    def is_grouped(query: ValuesQuery) -> bool:
        """Whether the rows are groups - an explicit ``.group_by()``, or an aggregate added after
        ``.values()``/``.values_list()``, which groups by the selected columns. An aggregate
        annotated before them is computed per model row, like Django.

        Args:
            query: The values query.

        Returns:
            True for a grouped query.
        """
        if query._group_bys:
            return True
        unused_alias_keys = query._get_unused_alias_keys(ValuesOutput.get_output_field_names(query))
        return any(
            QueryAnnotations.annotation_is_aggregate(query, annotation)
            for name, annotation in query._annotations.items()
            if name in query._grouping_annotation_names and name not in unused_alias_keys
        )

    @staticmethod
    def annotation_is_group_key(query: ValuesQuery, annotation: Expression | Term) -> bool:
        """Whether a selected annotation is part of the group key - neither an aggregate nor a
        window function, which are computed per group.

        Args:
            query: The values query.
            annotation: The annotation.

        Returns:
            True for a group key.
        """
        term = QueryAnnotations.get_annotation_term(query, annotation)
        return not term.contains_aggregate and not RowMultiplication.term_reads_window_function(term)

    @staticmethod
    def get_group_key_names(query: ValuesQuery) -> list[str]:
        """The names the rows are grouped by - the ``.group_by()`` fields, else the selected
        fields and the annotations that are neither aggregates nor window functions.

        Args:
            query: The values query.

        Returns:
            The names.
        """
        if query._group_bys:
            return list(query._group_bys)
        return [
            name
            for name in ValuesOutput.get_output_field_names(query)
            if name not in query._annotations
            or ValuesGrouping.annotation_is_group_key(query, query._annotations[name])
        ]

    @staticmethod
    def returns_one_row_per_source_row(query: ValuesQuery) -> bool:
        """Whether each row stands for one row of the source queryset - not grouped, not
        deduplicated over the selected columns, and no selected field, ordering or selected
        annotation crossing a to-many relation.

        Args:
            query: The values query.

        Returns:
            True when the rows are the source queryset's own rows.
        """
        if (query._distinct and not query._distinct_on) or ValuesGrouping.is_grouped(query):
            return False
        selected_field_lookups = QueryJoins.get_field_lookups(
            query, (name for name in ValuesOutput.get_output_field_names(query) if name not in query._annotations)
        )
        if any(
            AggregatedMultiValuedPaths.get_multi_valued_paths(query.model, lookup)
            for lookup in (*selected_field_lookups, *QueryJoins.get_ordering_lookups(query))
        ):
            return False
        return not RowMultiplication.get_row_multiplying_annotation_names(query, selected_only=False)
