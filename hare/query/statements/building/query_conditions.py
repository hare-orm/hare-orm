from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.expressions import Expression, ExpressionContext
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.statements.building.keyset_bounds import KeysetBounds
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.building.query_ordering import QueryOrdering
from hare.sql import JoinType, Order
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.statements.awaitable_query import AwaitableQuery


class QueryConditions:
    """The WHERE and the DISTINCT of a query: its filters resolved into criteria and their joins, and
    DISTINCT or DISTINCT ON over the columns and orderings it needs."""

    @staticmethod
    def get_filters(
        query: AwaitableQuery[Any],
        fields_for_select: Collection[str] | None = None,
        *,
        value_wrapper_references: (RecordedValueReferences | None) = None,
    ) -> None:
        """Builds the query's filters. ``value_wrapper_references``, when a plan is recorded, collects the
        value references in order: the annotations', the filters', then the keyset boundaries'.

        Args:
            query: The query.
            fields_for_select: The fields the query selects, None for all of them.
            value_wrapper_references: Collects the value references while a plan is recorded.
        """
        # Shared with QueryAnnotations.get_annotate(), which fills it first - a filter splitting an aggregated
        # to-many relation into a second JOIN is detected through it.
        aggregated_multi_valued_paths = AggregatedMultiValuedPaths()
        # Every to-many JOIN outside an aggregate - a non-aggregate annotation's, a filter's, a
        # values() field's or the ordering's - counts towards the fan-out check below.
        aggregated_multi_valued_paths.is_recording_row_joins = True
        query._has_aggregate = QueryAnnotations.get_annotate(
            query,
            fields_for_select,
            value_wrapper_references=value_wrapper_references,
            aggregated_multi_valued_paths=aggregated_multi_valued_paths,
        )

        modifier = QueryModifier()
        # Built once, not once per node - every field here is identical across the whole loop, and
        # Q.get_result() never mutates a ExpressionContext (frozen dataclass) it's handed.
        expression_context = ExpressionContext(
            model=query.model,
            dialect=query.dialect,
            connection=query._connection,
            table=query._effective_basetable(),
            annotations=query._annotations,
            value_wrapper_references=value_wrapper_references,
            # A filter crossing a Select(relation, extra_condition=...) relation builds its JOIN
            # with that condition.
            select_related_extra_conditions=query._select_related_extra_conditions,
            multi_valued_join_generations={},
            aggregated_multi_valued_paths=aggregated_multi_valued_paths,
            visibility=query._visibility,
            window_function_filter_allowed=query.window_filter_wrapping_supported,
        )
        window_filter_criterion: Criterion | None = None
        for node in query._q_objects:
            node_modifier = node.get_result(expression_context)
            node_criterion = node_modifier._and_criterion()
            if isinstance(node_criterion, Criterion) and RowMultiplication.term_reads_window_function(node_criterion):
                if not query.window_filter_wrapping_supported:
                    raise QueryError(
                        "Cannot filter on a window function (Window(...)) - SQL does not allow window "
                        "functions in WHERE or HAVING. Filter a .values()/.values_list() query instead, "
                        "which applies the filter to the query wrapped in a subquery, e.g. "
                        "Model.objects.filter(pk__in=Subquery(queryset.filter(...).values(<pk field>)))."
                    )
                window_filter_criterion = (
                    node_criterion if window_filter_criterion is None else window_filter_criterion & node_criterion
                )
                modifier &= QueryModifier(joins=node_modifier.joins)
            else:
                modifier &= node_modifier

        # A non-distinct aggregate is inflated by every other to-many JOIN of the query - rejected,
        # unless only the query's truthiness is read.
        if not query.aggregate_value_is_unused and aggregated_multi_valued_paths.aggregate_crossings:
            for lookup in query._get_row_join_lookups():
                aggregated_multi_valued_paths.record_lookup(query.model, lookup)
            for lookup in QueryJoins.get_non_null_filtered_lookups(query):
                aggregated_multi_valued_paths.record_non_null_lookup(query.model, lookup)
            for lookup in query._get_group_key_lookups():
                aggregated_multi_valued_paths.record_group_key_lookup(query.model, lookup)
            aggregated_multi_valued_paths.record_row_pinning_conditions(
                query.model,
                query._get_top_level_conditions_by_generation(),
                expression_context.multi_valued_join_generations or {},
            )
            if fan_out_message := aggregated_multi_valued_paths.get_fan_out_error_message():
                raise QueryError(fan_out_message)

        for join in modifier.joins:
            if join[0] not in query._joined_tables_set:
                query.query = query.query.join(join[0], how=JoinType.LEFT_OUTER).on(join[1])
                query._joined_tables.append(join[0])
                query._joined_tables_set.add(join[0])

        where_criterion = modifier.where_criterion
        if cursor_criterion := KeysetBounds.get_cursor_criterion(
            query, value_wrapper_references=value_wrapper_references
        ):
            if window_filter_criterion is not None:
                # Pagination applies to the rows left after the window filter.
                window_filter_criterion = window_filter_criterion & cursor_criterion
            else:
                where_criterion = where_criterion & cursor_criterion if where_criterion else cursor_criterion

        query.query._havings = modifier.having_criterion
        query.query._wheres = where_criterion
        query._window_filter_criterion = window_filter_criterion

    @staticmethod
    def get_distinct(
        query: AwaitableQuery[Any],
        distinct: bool,
        distinct_on: Sequence[str],
        orderings: Iterable[tuple[str, Order]],
        annotations: dict[str, Term | Expression],
    ) -> None:
        query.query._distinct = distinct
        query.query._distinct_on = []
        if not distinct:
            return
        orderings = query._apply_default_ordering(orderings, annotations)
        if not distinct_on:
            # A plain DISTINCT (no DISTINCT ON field list) - distinct_on's own
            # "leading ORDER BY fields" validation below doesn't apply here.
            QueryOrdering.include_orderbys_in_select(query)
        else:
            if not query.dialect.features.supports_distinct_on:
                raise UnSupportedError(f"distinct(*fields) is not supported by the {query.dialect} dialect")
            ordering_fields = [ordering[0] for ordering in orderings]
            length_ordering_fields = len(ordering_fields)
            # Compared the way the ordering was expanded - a relation by its key columns, "pk" by
            # every primary key field.
            distinct_on_ordering_fields = [
                ordering_field
                for distinct_on_name in distinct_on
                for ordering_field in (
                    QueryOrdering.get_relation_ordering_key_names(query.model, distinct_on_name)
                    or (
                        query.model._meta.primary_key_attribute_names
                        if distinct_on_name == "pk"
                        else (distinct_on_name,)
                    )
                )
            ]
            distinct_on_by_source_field = []
            for field_name in (
                concrete_field_path
                for distinct_on_name in distinct_on
                for concrete_field_path in (
                    (distinct_on_name,)
                    if distinct_on_name in annotations
                    else ConcreteFieldPaths.get_paths(query.model, distinct_on_name)
                )
            ):
                if field_name in annotations:
                    # The term ORDER BY orders the annotation by - DISTINCT ON must match it.
                    annotation_term = query._annotation_ordering_terms.get(field_name)
                    if annotation_term is None:
                        annotation_term = QueryAnnotations.get_annotation_expression_term(
                            query, field_name, annotations, query._effective_basetable()
                        )
                    distinct_on_by_source_field.append(annotation_term)
                    continue
                field_object = query.model._meta.fields_map.get(field_name)
                part_after = field_name
                related_table = query._effective_basetable()
                related_model: type[Model] = query.model
                path_prefix = ""
                while part_after:
                    related_field_name, __, part_after = part_after.partition("__")
                    if related_field_name in related_model._meta.fetch_fields:
                        # Each hop is looked up on the model reached so far, not on the query's own
                        # model.
                        related_field = cast(
                            "RelationalField[Model]", related_model._meta.fields_map[related_field_name]
                        )
                        path_prefix = f"{path_prefix}__{related_field_name}" if path_prefix else related_field_name
                        extra_condition = query._select_related_extra_conditions.get(path_prefix)
                        related_table = QueryJoins.join_table_by_field(
                            query, related_table, related_field_name, related_field, extra_condition
                        )
                        related_model = related_field.related_model
                    else:
                        field_object = related_model._meta.fields_map.get(related_field_name)

                        if not field_object:
                            raise FieldError(f"Unknown field {related_field_name} for model {related_model.__name__}")
                        EncryptedFieldBase.raise_if_encrypted(field_object, "distinct(*fields)")
                        related_table_field: Term = related_table[field_object.source_field or related_field_name]
                        if function_cast := field_object.get_function_cast(query.dialect):
                            related_table_field = function_cast(field_object, related_table_field)
                        distinct_on_by_source_field.append(related_table_field)
            # Checked once every name resolved - an unknown one is a FieldError, not a mismatch.
            for position, field in enumerate(distinct_on_ordering_fields):
                if ordering_fields and (position >= length_ordering_fields or ordering_fields[position] != field):
                    raise QueryError(
                        f"distinct(*fields) must match the leading order_by() fields. "
                        f"Expected order_by() to start with {distinct_on!r}."
                    )
            query.query = query.query.distinct_on(*distinct_on_by_source_field)
