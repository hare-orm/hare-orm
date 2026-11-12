from __future__ import annotations

import dataclasses
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.query.constants import AGGREGATE_OVER_DISTINCT_ON_MESSAGE
from hare.query.execution.query_execution import QueryExecution
from hare.query.expressions import Expression, ExpressionContext, Q
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.constants import NO_CALL_PLAN
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plan_parts import PlanParts
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.statement.declarations import CallPlanHit
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.queryset.annotation_access_tracker import AnnotationAccessTracker
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.query.statements.building.query_conditions import QueryConditions
from hare.query.statements.building.query_ctes import QueryCtes
from hare.query.statements.building.query_grouping import QueryGrouping
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.constants import (
    AGGREGATE_FIELD_NAMED_METRIC_PREFIX,
    AGGREGATE_SUBQUERY_ALIAS,
    AGGREGATE_SUBQUERY_GROUP_BY_ALIAS_PREFIX,
    AGGREGATE_SUBQUERY_PRIMARY_KEY_ALIAS_PREFIX,
    AGGREGATE_UNGROUPED_METRIC_MESSAGE,
    AGGREGATE_UNSELECTED_METRIC_MESSAGE,
)
from hare.query.statements.projections.derived_projection import DerivedProjection
from hare.query.statements.projections.projected_value import ProjectedValue
from hare.query.statements.summary.rows_summary_query import RowsSummaryQuery
from hare.sql.builder.queries.correlated_subqueries import CorrelatedSubqueries
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.functions.aggregate_function import AggregateFunction
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.field import Field
    from hare.query.queryset.queryset import QuerySet
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.builder.tables.selectable import Selectable


class AggregateQuery(RowsSummaryQuery):
    """``QuerySet.aggregate()`` - aggregate expressions computed across the whole queryset, always one
    row.

    Only the prior annotations the metrics or the filters read take part. When one of them is an
    aggregate, the per-row aggregates are computed in a derived table grouped by the primary key.
    After ``.group_by()`` the metrics run over the grouped rows; built from a ``.values()`` query or
    a set operation, over that query as a derived table.
    """

    rows_source_method_name = "_get_aggregate_source"

    #: A plain SELECT of aggregate expressions - see AwaitableQuery.is_read_only.
    is_read_only: ClassVar[bool] = True

    __slots__ = ("_metric_keys",)

    #: The keyset boundary compares in the ordering's directions (.before_cursor() reverses them).
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_metric_keys", PlanKeyForm.VALUE),
        ("_is_none", PlanKeyForm.VALUE),
        ("_orderings", PlanKeyForm.TUPLE),
        *StatementPlanDescriptions.ROWS_SLOTS,
        *StatementPlanDescriptions.CURSOR_SLOTS,
    )

    keeps_grouping: ClassVar[bool] = True

    def __init__(
        self,
        source: QuerySpecification[Any],
        metrics: Mapping[str, Expression | Term],
        *,
        rows_query: AwaitableQuery[Any] | None = None,
        selected_rows: QuerySet[Any] | None = None,
    ) -> None:
        """
        Args:
            source: The queryset aggregated, or a query made from one.
            metrics: The expressions to compute, by the name each is returned under.
            rows_query: The rows the metrics run over, as a derived table - sliced itself.
            selected_rows: A ``.values()`` or set-operation queryset whose rows the metrics run over -
                made into its aggregate the first time the query is built.
        """
        super().__init__(
            source,
            rows_query=rows_query,
            selected_rows=selected_rows,
            selected_rows_arguments=dict(metrics) if selected_rows is not None else None,
        )
        self._apply_summary_arguments(dict(metrics))

    def _apply_summary_arguments(self, arguments: dict[str, Any]) -> None:
        """Bind metrics to the chosen source while leaving its filters' annotations intact."""
        metrics = arguments
        if self._rows_query is not None or self._selected_rows is not None:
            self._limit = None
            self._offset = 0
        self._raise_if_annotation_names_are_unsafe(metrics)
        metrics = self._get_metrics_apart_from_columns(metrics)
        # The queryset's own .annotate()/.alias() keys stay, so a filter reading one of them still
        # resolves - only the metrics are requested and returned (see _execute()).
        self._annotations = {**self._annotations, **metrics}
        self._metric_keys = frozenset(metrics)

    def _get_filter_annotations(self) -> dict[str, Any]:
        # The filters came before aggregate() - a metric named as a field (aggregate(i=Sum("i"))) leaves
        # filter(i__gt=1) reading the field.
        return {key: value for key, value in self._annotations.items() if key not in self._metric_keys}

    def _get_metrics_apart_from_columns(
        self, metrics: Mapping[str, Expression | Term]
    ) -> dict[str, Expression | Term]:
        """The metrics, one named as a column of the model under ``AGGREGATE_FIELD_NAMED_METRIC_PREFIX`` - so
        no filter or join reads it for the column. ``_execute()`` returns it under its own name.

        Args:
            metrics: The metrics, by the name each is returned under.

        Returns:
            The metrics by the name each is selected as.
        """
        column_names = self.model._meta.fields_db_projection
        return {
            f"{AGGREGATE_FIELD_NAMED_METRIC_PREFIX}{name}"
            if name in column_names or name in column_names.values()
            else name: metric
            for name, metric in metrics.items()
        }

    def _prepare_build(self) -> None:
        """Prepare the bound rows and retain only annotations the aggregate computation reads.

        The declaration on the originating queryset is untouched. The resulting annotation
        mapping is fixed for the entire build and supplies the ordinary statement description,
        including every parameter origin used by either side of a derived query.
        """
        super()._prepare_build()
        if self._bound_rows_query is not None:
            return
        annotations = self._annotations
        metrics = [annotation for name, annotation in annotations.items() if name in self._metric_keys]
        grouped = [name for name in self._group_bys if name in annotations]
        referenced = self._get_referenced_annotation_keys(
            annotations, [*metrics, *(annotations[name] for name in grouped)], self._q_objects
        )
        used = self._metric_keys | set(referenced) | set(grouped)
        self._annotations = {name: annotation for name, annotation in annotations.items() if name in used}
        self._alias_keys = self._alias_keys.intersection(self._annotations)

    def _get_call_signature_type_part(self) -> tuple[Any, Sequence[Any]]:
        # The metrics - their structure in the key, their values bound after the calls'.
        if self._bound_rows_query is not None:
            return None, ()
        description = self._get_metrics_plan_description()
        if description is None:
            return None, ()
        return description.structure, list(description.values)

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        if self._bound_rows_query is not None:
            return self._build_statement_over_rows(value_wrapper_references)
        self.query = self._get_base_query()
        QueryJoins.apply_effective_basetable(self)

        # Only the metrics are selected - a non-aggregate annotation next to a bare aggregate would
        # need a GROUP BY. No automatic GROUP BY: aggregate() gives one row.
        keeps_plan = True
        prior_annotation_keys = [key for key in self._annotations if key not in self._metric_keys]
        if self._group_bys:
            if not self._make_query_over_group_by_subquery(value_wrapper_references):
                keeps_plan = False
        elif prior_annotation_keys or self._distinct or self._distinct_on:
            self._make_query_from_referenced_annotations(value_wrapper_references)
        else:
            self._apply_filters_and_check_aggregates(
                self._metric_keys, value_wrapper_references=value_wrapper_references
            )
            QueryCtes.apply_with_ctes(self, value_wrapper_references=value_wrapper_references)
        if self._is_none:
            # A .none() aggregate still runs, with an always-false condition - the database gives
            # each expression its value for an empty set.
            self.query = self.query.where(BasicCriterion.always_false())
        return keeps_plan

    def _apply_filters_and_check_aggregates(
        self,
        fields_for_select: Collection[str],
        *,
        value_wrapper_references: RecordedValueReferences | None = None,
    ) -> None:
        """Builds the SELECT/JOIN/WHERE/HAVING parts of the query, then rejects a SELECTed
        aggregate that nests another aggregate function inside itself.

        Args:
            fields_for_select: The annotation keys to SELECT.
            value_wrapper_references: The references being recorded, None when the query keeps no plan.

        Raises:
            QueryError: A selected expression is an aggregate over an aggregate or a window
                function.
        """
        QueryConditions.get_filters(self, fields_for_select, value_wrapper_references=value_wrapper_references)
        for select_term in self.query._selects:
            nodes: Iterator[Any] = select_term.nodes_()
            for node in nodes:
                if (
                    isinstance(node, AggregateFunction)
                    and not node.is_analytic
                    and any(RowMultiplication.term_reads_window_function(argument) for argument in node.args)
                ):
                    raise QueryError(
                        f"{select_term.alias!r} is an aggregate function over a window function "
                        "(Window(...)) annotation - SQL cannot aggregate a window function in the query "
                        "computing it. Fetch the windowed values with .values_list(...) and aggregate them "
                        "in Python instead."
                    )
                if isinstance(node, AggregateFunction) and any(
                    self._contains_aggregate_function(argument) for argument in node.args
                ):
                    raise QueryError(
                        f"{select_term.alias!r} is an aggregate function over another aggregate "
                        "(for example Sum('n') where the annotation 'n' is itself a Count(...)) - SQL "
                        "cannot nest them. Compute the outer one in .aggregate(), e.g. "
                        ".annotate(n=Count(...)).aggregate(total=Sum('n'))."
                    )

    @staticmethod
    def _contains_aggregate_function(term: Term) -> bool:
        """Whether `term` or anything nested in it is an aggregate function call."""
        nodes: Iterator[Any] = term.nodes_()
        return any(isinstance(node, AggregateFunction) for node in nodes)

    def _get_referenced_annotation_keys(
        self,
        annotations: dict[str, Any],
        root_expressions: Iterable[Expression | Term],
        q_objects: Iterable[Q],
    ) -> list[str]:
        """Finds the annotation keys `root_expressions` and `q_objects` read, transitively - an
        annotation reached through another one's reference is resolved against the same tracking
        mapping, so its own references are recorded too.

        Args:
            annotations: The annotation mapping to resolve against.
            root_expressions: The expressions whose references are wanted (the requested metrics).
            q_objects: The filters whose references are wanted.

        Returns:
            The referenced keys, in `annotations` order - a JOIN repeating rows (``ArrayJoin``) with them,
            read or not.
        """
        tracker = AnnotationAccessTracker(annotations)
        for expression in root_expressions:
            if isinstance(expression, Expression):
                expression.get_result(self._get_probe_expression_context(tracker))
        for q_object in q_objects:
            q_object.get_result(self._get_probe_expression_context(tracker))
        return [
            key
            for key, annotation in annotations.items()
            if key in tracker.accessed_keys or getattr(annotation, "repeats_rows", False)
        ]

    def _get_annotation_value_field(
        self, annotation: Expression | Term, annotations: dict[str, Any]
    ) -> Field[Any] | None:
        """The field an annotation's value decodes through, e.g. an integer one for `Count(...)`.

        Args:
            annotation: The annotation.
            annotations: The annotation mapping it is resolved against.

        Returns:
            The value field, or None when the annotation has no known type.
        """
        if not isinstance(annotation, Expression):
            return None
        return annotation.get_value_field(annotation.get_result(self._get_probe_expression_context(annotations)))

    def _make_query_from_referenced_annotations(
        self, value_wrapper_references: RecordedValueReferences | None
    ) -> None:
        """Builds the query of a queryset carrying annotations or ``.distinct()``. With no referenced
        aggregate annotation and no ``.distinct()`` it is a plain one-row aggregate; otherwise the
        rows are first reduced to one per base row in a grouped derived table.

        Args:
            value_wrapper_references: The references being recorded, None when the query keeps no plan.

        Raises:
            QueryError: The queryset is ``.distinct(<fields>)``.
        """
        if self._distinct_on:
            raise QueryError(AGGREGATE_OVER_DISTINCT_ON_MESSAGE)
        annotations = self._annotations
        metric_annotations = [annotation for key, annotation in annotations.items() if key in self._metric_keys]
        referenced_keys = self._get_referenced_annotation_keys(annotations, metric_annotations, self._q_objects)
        aggregate_keys = [
            key for key in referenced_keys if QueryAnnotations.annotation_is_aggregate(self, annotations[key])
        ]
        if aggregate_keys or self._distinct:
            self._make_query_over_grouped_subquery(referenced_keys, aggregate_keys, value_wrapper_references)
        else:
            self._apply_filters_and_check_aggregates(
                self._metric_keys, value_wrapper_references=value_wrapper_references
            )
            QueryCtes.apply_with_ctes(self, value_wrapper_references=value_wrapper_references)

    def _new_build(self, annotations: dict[str, Any], *, with_conditions: bool = True) -> AggregateQuery:
        """Create an independent build scope with explicit annotations and row conditions."""
        prepared = copy(self)
        prepared._plan_origin = self
        prepared._annotations = annotations
        QueryJoins.reset_joined_tables(prepared)
        prepared._annotation_output_fields = {}
        prepared.query = prepared._get_base_query()
        QueryJoins.apply_effective_basetable(prepared)
        if not with_conditions:
            prepared._q_objects = []
            prepared._cursor_values = ()
            prepared._before_cursor_values = ()
        return prepared

    def _take_build(self, prepared: AggregateQuery) -> None:
        """Retain the final statement's bound results without replacing its declaration."""
        self.query = prepared.query
        self._annotation_output_fields = prepared._annotation_output_fields
        self._annotation_expression_results = prepared._annotation_expression_results
        self._annotation_ordering_terms = prepared._annotation_ordering_terms
        self._expression_term_references = prepared._expression_term_references
        self._joined_tables = prepared._joined_tables
        self._joined_tables_set = prepared._joined_tables_set
        self._has_aggregate = prepared._has_aggregate
        self._window_filter_criterion = prepared._window_filter_criterion

    def _make_query_over_grouped_subquery(
        self,
        referenced_keys: list[str],
        aggregate_keys: list[str],
        value_wrapper_references: RecordedValueReferences | None,
    ) -> None:
        """Computes the metrics over the base table joined to a derived table carrying the referenced
        aggregate annotations - one row per base row passing the filters.

        Args:
            referenced_keys: Every prior annotation the metrics or the filters read.
            aggregate_keys: Those of them that are aggregates.
            value_wrapper_references: The references being recorded, None when the query keeps no plan.
        """
        annotations = self._annotations
        effective_table = self._effective_basetable()
        primary_key_columns = [
            self.model._meta.fields_db_projection[attribute_name]
            for attribute_name in self.model._meta.primary_key_attribute_names
        ]

        inner = self._new_build({key: annotation for key, annotation in annotations.items() if key in referenced_keys})
        inner._apply_filters_and_check_aggregates(aggregate_keys, value_wrapper_references=value_wrapper_references)
        for index, column in enumerate(primary_key_columns):
            inner.query = inner.query.select(
                effective_table[column].as_(f"{AGGREGATE_SUBQUERY_PRIMARY_KEY_ALIAS_PREFIX}{index}")
            )
        inner.query = inner.query.groupby(*(effective_table[column] for column in primary_key_columns))
        projection = DerivedProjection(
            inner.query,
            output_fields={
                key: inner._get_annotation_value_field(annotations[key], annotations) for key in aggregate_keys
            },
        )
        grouped_subquery = projection.query.as_(AGGREGATE_SUBQUERY_ALIAS)
        outer_annotations = {
            key: ProjectedValue(grouped_subquery[projection.by_alias[key].name], projection.by_alias[key].field)
            if key in aggregate_keys
            else annotation
            for key, annotation in annotations.items()
        }
        metric_annotations = [annotation for key, annotation in annotations.items() if key in self._metric_keys]
        referenced = self._get_referenced_annotation_keys(outer_annotations, metric_annotations, ())
        outer = self._new_build(
            {
                key: annotation
                for key, annotation in outer_annotations.items()
                if key in self._metric_keys or key in aggregate_keys or key in referenced
            },
            with_conditions=False,
        )
        join_criterion = None
        for index, column in enumerate(primary_key_columns):
            column_criterion = effective_table[column].eq(
                grouped_subquery[f"{AGGREGATE_SUBQUERY_PRIMARY_KEY_ALIAS_PREFIX}{index}"]
            )
            join_criterion = column_criterion if join_criterion is None else join_criterion & column_criterion
        outer.query = outer.query.join(grouped_subquery).on(join_criterion)
        QueryConditions.get_filters(outer, self._metric_keys, value_wrapper_references=value_wrapper_references)
        QueryCtes.apply_with_ctes(outer, value_wrapper_references=value_wrapper_references)
        self._take_build(outer)

    def _make_query_over_group_by_subquery(self, value_wrapper_references: RecordedValueReferences | None) -> bool:
        """Computes the metrics over the rows of a ``.group_by()`` queryset - a derived table with
        one row per group, carrying its group-by columns and the aggregate annotations the metrics
        read, like ``.group_by(...).values(...)`` returns them.

        Args:
            value_wrapper_references: The references being recorded, None when the query keeps no plan.

        Returns:
            Whether all metrics can describe their parameter sources for a cached plan.

        Raises:
            QueryError: The queryset is ``.distinct(<fields>)``.
            QueryError: A metric reads a column that isn't grouped by, or a relation.
        """
        if self._distinct_on:
            raise QueryError(AGGREGATE_OVER_DISTINCT_ON_MESSAGE)
        annotations = self._annotations
        grouped_annotation_names = [name for name in self._group_bys if name in annotations]
        metric_annotations = [annotation for key, annotation in annotations.items() if key in self._metric_keys]
        referenced_keys = self._get_referenced_annotation_keys(
            annotations,
            [*metric_annotations, *(annotations[name] for name in grouped_annotation_names)],
            self._q_objects,
        )
        referenced_keys = [key for key in annotations if key in referenced_keys or key in grouped_annotation_names]
        aggregate_keys = [
            key for key in referenced_keys if QueryAnnotations.annotation_is_aggregate(self, annotations[key])
        ]
        grouped_subquery_columns: dict[str, tuple[str, Field[Any] | None]] = {}
        inner = self._new_build({key: annotation for key, annotation in annotations.items() if key in referenced_keys})
        inner._apply_filters_and_check_aggregates(
            [*aggregate_keys, *grouped_annotation_names], value_wrapper_references=value_wrapper_references
        )
        for key in (*aggregate_keys, *grouped_annotation_names):
            grouped_subquery_columns[key] = (key, inner._get_annotation_value_field(annotations[key], annotations))
        group_by_terms = QueryGrouping.get_group_bys(inner, *self._group_bys)
        grouped_field_names = [
            path
            for name in self._group_bys
            for path in ((name,) if name in annotations else ConcreteFieldPaths.get_paths(self.model, name))
        ]
        for index, (field_name, group_by_term) in enumerate(zip(grouped_field_names, group_by_terms, strict=True)):
            if field_name in annotations:
                continue
            column_alias = f"{AGGREGATE_SUBQUERY_GROUP_BY_ALIAS_PREFIX}{index}"
            inner.query = inner.query.select(group_by_term.as_(column_alias))
            grouped_subquery_columns[field_name] = (
                column_alias,
                QueryAnnotations.get_field_object_by_path(inner, field_name),
            )
        inner.query._groupbys = group_by_terms
        projection = DerivedProjection(
            inner.query, output_fields={alias: field for alias, field in grouped_subquery_columns.values()}
        )
        grouped_subquery = projection.query.as_(AGGREGATE_SUBQUERY_ALIAS)

        QueryJoins.reset_joined_tables(self)
        self.query = self._connection.query_class.from_(grouped_subquery)
        outer_annotations = {
            **annotations,
            **{
                name: ProjectedValue(grouped_subquery[column_alias], output_field)
                for name, (column_alias, output_field) in grouped_subquery_columns.items()
            },
        }
        outer_expression_context = dataclasses.replace(
            self._get_probe_expression_context(outer_annotations), value_wrapper_references=value_wrapper_references
        )
        for key, annotation in annotations.items():
            if key not in self._metric_keys:
                continue
            self.query._select_other(
                self._get_metric_over_derived_table(
                    key, annotation, outer_expression_context, grouped_subquery, AGGREGATE_UNGROUPED_METRIC_MESSAGE
                )
            )
        QueryCtes.apply_with_ctes(self, value_wrapper_references=value_wrapper_references)
        return self._get_metrics_plan_description() is not None

    def _get_rows_summary_description(self) -> PlanDescription | None:
        return self._get_metrics_plan_description()

    def _get_metrics_plan_description(self) -> PlanDescription | None:
        """Describes the metrics resolved over the rows of a rows query or of a ``.group_by()``
        queryset - each metric's name and structure, and its values, in the order they are resolved.
        A name they read is one of the rows' columns, holding no values.

        Returns:
            The description, None when a metric keeps no plan.
        """
        structures = []
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        for key, annotation in self._annotations.items():
            if key not in self._metric_keys:
                continue
            if not isinstance(annotation, Expression):
                return None
            description = annotation.get_plan_description(PlanContext(model=self.model))
            if description is None:
                return None
            structures.append((key, description.structure))
            values += description.values
            if origins is not None:
                origins = PlanParts.get_origins(description, origins)
        return PlanDescription(tuple(structures), values, origins)

    def _get_query_over_rows(
        self, built_rows_query: AwaitableQuery[Any], value_wrapper_references: RecordedValueReferences | None
    ) -> QueryBuilder:
        """Computes the metrics over the rows of a ``.values()``/``.values_list()`` query (or set
        operation) as a derived table - a metric reads a selected column by its output name, or by
        the field name it selects when that name isn't an output name too.

        Args:
            built_rows_query: The query, built.
            value_wrapper_references: The list the metrics' value references are recorded into - None when
                no plan is recorded.

        Returns:
            The query computing the metrics.

        Raises:
            QueryError: A metric reads a column that isn't selected, or a relation.
        """
        output_columns = built_rows_query._get_output_columns()  # type: ignore[attr-defined]
        projection = DerivedProjection(
            built_rows_query.query,
            output_fields={alias: field for _, _, alias, field in output_columns},
        )
        inner_query = projection.query
        with_clauses, inner_query._with = inner_query._with, []
        rows_subquery = inner_query.as_(AGGREGATE_SUBQUERY_ALIAS)
        column_annotations: dict[str, Any] = {}
        for output_name, _selected_name, alias, output_field in output_columns:
            column = projection.by_alias[alias]
            column_annotations[output_name] = ProjectedValue(rows_subquery[column.name], column.field)
        for _output_name, selected_name, alias, output_field in output_columns:
            if selected_name not in column_annotations:
                column = projection.by_alias[alias]
                column_annotations[selected_name] = ProjectedValue(rows_subquery[column.name], column.field)
        QueryJoins.reset_joined_tables(self)
        query = self._connection.query_class.from_(rows_subquery)
        # Track declared references too: Count(*) no longer carries a base-table column
        # after resolution, but still cannot stand in for an unselected annotation.
        unselected_annotations = {
            name: annotation
            for name, annotation in getattr(self._rows_query, "_annotations", {}).items()
            if name not in column_annotations
        }
        tracker = AnnotationAccessTracker({**unselected_annotations, **self._annotations, **column_annotations})
        outer_expression_context = dataclasses.replace(
            self._get_probe_expression_context(tracker),
            value_wrapper_references=value_wrapper_references,
        )
        for key, annotation in self._annotations.items():
            if key not in self._metric_keys:
                continue
            tracker.accessed_keys.clear()
            query._select_other(
                self._get_metric_over_derived_table(
                    key, annotation, outer_expression_context, rows_subquery, AGGREGATE_UNSELECTED_METRIC_MESSAGE
                )
            )
            if tracker.accessed_keys.intersection(unselected_annotations):
                raise QueryError(AGGREGATE_UNSELECTED_METRIC_MESSAGE.format(key=key))
        query._with = with_clauses
        if self._is_none:
            query = query.where(BasicCriterion.always_false())
        return query

    def _get_metric_over_derived_table(
        self,
        key: str,
        annotation: Expression | Term,
        context: ExpressionContext,
        table: Selectable,
        error_message: str,
    ) -> Term:
        """Build a named metric using only the columns exposed by a derived table.

        Plain SQL terms keep their declared references. Expressions report joins and output fields;
        a reference outside the table is rejected with the caller's grouped or selected-row message.
        """
        if isinstance(annotation, Term) and not isinstance(annotation, Expression):
            term = annotation
        else:
            result = annotation.get_result(context)
            term = result.term
            if self.reads_outside_derived_table(result.joins, term, table):
                raise QueryError(error_message.format(key=key))
            if getattr(annotation, "populate_field_object", False):
                self._annotation_output_fields[key] = annotation.get_value_field(result)
        return term.as_(key)

    @staticmethod
    def reads_outside_derived_table(joins: list[Any], term: Term, derived_table: Any) -> bool:
        """Whether a metric over a derived table's rows reads anything but its columns - a relation, or
        a column of another table.

        Args:
            joins: The joins resolving the metric asked for.
            term: The resolved metric.
            derived_table: The derived table.

        Returns:
            True for such a metric.
        """
        return bool(joins) or CorrelatedSubqueries.reads_other_tables(term, {derived_table})

    async def _execute_bound(self) -> dict[str, Any]:
        if not self._metric_keys:
            return {}
        lookup = NO_CALL_PLAN
        connection: DatabaseClient | None = None
        if self._call_signature is not None:
            connection = self.get_connection()
            call_plan = self._find_call_plan(connection)
            if isinstance(call_plan, CallPlanHit):
                return await QueryExecution.read(
                    self,
                    connection,
                    self._aggregate(
                        connection, call_plan.sql, call_plan.parameters, dict(call_plan.plan.annotation_output_fields)
                    ),
                )
            lookup = call_plan
        query = self._get_execution_query_to_build(lookup, connection)
        statement = query._make_query_to_run()
        return await QueryExecution.read(
            query,
            query._connection,
            query._aggregate(query._connection, *statement, query._annotation_output_fields),
        )

    async def _aggregate(
        self,
        connection: DatabaseClient,
        sql: str,
        parameters: list[Any],
        annotation_output_fields: Mapping[str, Field[Any] | None],
    ) -> dict[str, Any]:
        """Runs the statement and reads the metrics off its one row.

        Args:
            connection: This execution's connection.
            sql: The statement.
            parameters: Its parameters.
            annotation_output_fields: The field each metric's value is read by, when it has one.

        Returns:
            The metrics, in the order they were passed.
        """
        result = await connection.execute_dicts(sql, parameters, retry_read=True)
        row = result[0] if result else dict.fromkeys(self._metric_keys)
        for key in self._metric_keys:
            # Mirrors ValuesOutput.get_selected_value_field()'s identical read of this same
            # per-query-execution dict, populated by QueryAnnotations.get_annotate() only for the specific
            # functions that opt in (e.g. Max/Min - result type equals the source field's type).
            field_object = annotation_output_fields.get(key)
            if field_object:
                row[key] = connection.dialect.types.get_python_value(field_object, row[key])
        # In the order the metrics were passed, like Django - self._metric_keys is a set.
        ordered_metric_keys = [key for key in self._annotations if key in self._metric_keys]
        ordered_metric_keys += sorted(self._metric_keys.difference(ordered_metric_keys))
        return {key.removeprefix(AGGREGATE_FIELD_NAMED_METRIC_PREFIX): row[key] for key in ordered_metric_keys}
