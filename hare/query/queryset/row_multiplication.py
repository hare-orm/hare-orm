from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.query.expressions import (
    Expression,
    ExpressionContext,
)
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.query_connection import QueryConnection
from hare.sql.builder.tables.selectable import Selectable
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.query.queryset.query_specification import QuerySpecification


class RowMultiplication:
    """Which annotations of a query multiply its rows - an aggregate over a to-many relation, a window
    function - and which a GROUP BY may hold."""

    @staticmethod
    def ordering_joins_multi_valued_relation(specification: QuerySpecification[Any]) -> bool:
        """Whether removing the effective ordering would remove a join that repeats rows.

        Reads explicit and default orderings, including expressions registered as annotations.
        Such a join must survive when a summary or a set operation embeds the query.
        """
        for name, _order in specification._apply_default_ordering(specification._orderings):
            annotation = specification._annotations.get(name)
            if annotation is None:
                annotation = specification.model._meta.ordering_expressions.get(name)
            if annotation is None:
                if AggregatedMultiValuedPaths.get_multi_valued_paths(specification.model, name):
                    return True
            elif isinstance(annotation, Expression):
                paths = AggregatedMultiValuedPaths()
                paths.is_recording_row_joins = True
                annotation.get_result(specification._get_probe_expression_context(specification._annotations, paths))
                if paths.row_paths:
                    return True
        return False

    @staticmethod
    def get_row_multiplying_annotation_names(
        specification: QuerySpecification[Any], *, selected_only: bool
    ) -> list[str]:
        """Names of the annotations reading a to-many relation outside any aggregate - the query
        returns a row per related row for each of them - and of the named JOINs repeating the rows
        (``NamedJoin.repeats_rows``: a ``Lateral`` of several rows, an ``ArrayJoin``).

        Args:
            specification: The query specification - a queryset or a query made from one.
            selected_only: Leave out the ``.alias()`` annotations, which aren't selected.

        Returns:
            The annotation names, in annotation order.
        """
        annotation_names: list[str] = []
        unused_alias_keys = set() if selected_only else specification._get_unused_alias_keys()
        for annotation_name, annotation in specification._annotations.items():
            if not isinstance(annotation, Expression) or (
                selected_only and annotation_name in specification._alias_keys
            ):
                continue
            if annotation_name in unused_alias_keys:
                continue
            if getattr(annotation, "repeats_rows", False):
                annotation_names.append(annotation_name)
                continue
            multi_valued_paths = AggregatedMultiValuedPaths()
            multi_valued_paths.is_recording_row_joins = True
            annotation.get_result(
                ExpressionContext(
                    model=specification.model,
                    dialect=QueryConnection.get_analysis_dialect(specification),
                    connection=specification._connection,
                    table=specification._effective_basetable(),
                    annotations=specification._annotations,
                    annotation_names_in_progress={annotation_name},
                    select_related_extra_conditions=specification._select_related_extra_conditions,
                    aggregated_multi_valued_paths=multi_valued_paths,
                    visibility=specification._visibility,
                )
            )
            if multi_valued_paths.row_paths:
                annotation_names.append(annotation_name)
        return annotation_names

    @staticmethod
    def rows_repeat_per_primary_key(specification: QuerySpecification[Any]) -> bool:
        """Whether the query can return one primary key several times - a filter or annotation joins a
        to-many relation (with ``.distinct()``, only a selected annotation keeps the rows apart).
        Keyset paging would drop repeats across a page boundary.

        Args:
            specification: The query specification - a queryset or a query made from one.

        Returns:
            True if a primary key can repeat.
        """
        if specification._distinct:
            return bool(RowMultiplication.get_row_multiplying_annotation_names(specification, selected_only=True))
        if RowMultiplication.filters_join_multi_valued_relation(specification):
            return True
        return bool(RowMultiplication.get_row_multiplying_annotation_names(specification, selected_only=False))

    @staticmethod
    def filters_join_multi_valued_relation(specification: QuerySpecification[Any]) -> bool:
        """Whether resolving the filters joins a to-many relation into the query - also through
        an ``OuterReference("relation__field")`` of an ``Exists(...)``/``Subquery(...)`` condition -
        other than one a filter narrows to one related row (``tags=1``).

        Args:
            specification: The query specification - a queryset or a query made from one.

        Returns:
            True if a filter joins a to-many relation that can repeat a row.
        """
        multi_valued_paths = AggregatedMultiValuedPaths()
        multi_valued_paths.is_recording_row_joins = True
        claiming_generation_by_path: dict[str, int] = {}
        for q_object in specification._q_objects:
            q_object.get_result(
                specification._get_probe_expression_context(
                    specification._annotations,
                    multi_valued_paths,
                    multi_valued_join_generations=claiming_generation_by_path,
                )
            )
        multi_valued_paths.record_row_pinning_conditions(
            specification.model, specification._get_top_level_conditions_by_generation(), claiming_generation_by_path
        )
        return bool(set(multi_valued_paths.row_paths) - multi_valued_paths.pinned_paths)

    @staticmethod
    def reads_window_function(specification: QuerySpecification[Any]) -> bool:
        """Whether an annotation the query reads is computed by a window function - over every
        row the query matches, so a keyset page condition would change its value.

        Args:
            specification: The query specification - a queryset or a query made from one.

        Returns:
            True when a window function is read.
        """
        unused_alias_keys = specification._get_unused_alias_keys(specification._get_selected_field_names())
        probe_context = specification._get_probe_expression_context(specification._annotations)
        for annotation_name, annotation in specification._annotations.items():
            if annotation_name in unused_alias_keys:
                continue
            term = annotation.get_result(probe_context).term if isinstance(annotation, Expression) else annotation
            if isinstance(term, Term) and RowMultiplication.term_reads_window_function(term):
                return True
        return False

    @staticmethod
    def term_reads_window_function(term: Term) -> bool:
        """Whether a term reads a window function of its own query.

        Args:
            term: The term.

        Returns:
            True when a window function call appears outside any subquery.
        """
        boundary_node_ids: set[int] = set()
        nodes: Iterator[Any] = term.nodes_()
        for node in nodes:
            if id(node) in boundary_node_ids:
                continue
            if node.is_analytic:
                return True
            if node.is_subquery or isinstance(node, Selectable):
                boundary_node_ids.update(map(id, node.nodes_()))
        return False
