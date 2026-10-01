from __future__ import annotations

from collections.abc import Collection, Generator, Iterable, Iterator, Mapping
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import QueryError
from hare.query.constants import (
    AGGREGATE_SUBQUERY_ALIAS,
    AGGREGATE_SUBQUERY_GROUP_BY_ALIAS_PREFIX,
    AGGREGATE_SUBQUERY_PRIMARY_KEY_ALIAS_PREFIX,
)
from hare.query.enums import RecordedBuildStep
from hare.query.expressions import Expression, Q
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.statement_plan import StatementPlan
from hare.query.queryset.query_spec import QuerySpec
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.summary.rows_summary_query import RowsSummaryQuery
from hare.sql.enums import Equality
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.field import Field as SqlField
from hare.sql.terms.functions.aggregate_function import AggregateFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.sql.queries.builder.query_builder import QueryBuilder


class AggregateQuery(RowsSummaryQuery):
    """``QuerySet.aggregate()`` - aggregate expressions computed across the whole queryset, always one
    row.

    Only the prior annotations the metrics or the filters read take part. When one of them is an
    aggregate, the per-row aggregates are computed in a derived table grouped by the primary key.
    After ``.group_by()`` the metrics run over the grouped rows; built from a ``.values()`` query or
    a set operation, over that query as a derived table.
    """

    #: A plain SELECT of aggregate expressions - see AwaitableQuery.is_read_only.
    is_read_only: ClassVar[bool] = True

    __slots__ = ("_metric_keys", "_recorded_steps")

    class GroupedSubqueryColumn(Expression):
        """A grouped derived table's column standing in for an aggregate annotation, typed by
        that annotation's own value field."""

        plannable = False

        def __init__(self, term: Term, output_field: Field[Any] | None) -> None:
            self.term = term
            self.output_field = output_field

        def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
            return ExpressionResult(term=self.term, output_field=self.output_field)

    keeps_grouping: ClassVar[bool] = True

    def __init__(
        self,
        source: QuerySpec[Any],
        metrics: Mapping[str, Expression | Term],
        *,
        rows_query: AwaitableQuery[Any] | None = None,
    ) -> None:
        """
        Args:
            source: The queryset aggregated, or a query made from one.
            metrics: The expressions to compute, by the name each is returned under.
            rows_query: The rows the metrics run over, as a derived table - sliced itself.
        """
        super().__init__(source, rows_query=rows_query)
        if rows_query is not None:
            self._limit = None
            self._offset = 0
        self._raise_if_annotation_names_are_unsafe(metrics)
        # The queryset's own .annotate()/.alias() keys stay, so a filter reading one of them still
        # resolves - only the metrics are requested and returned (see _execute()).
        self._annotations = {**self._annotations, **metrics}
        self._metric_keys = frozenset(metrics)
        # The steps of the last build that recorded values - kept with the plan.
        self._recorded_steps: list[tuple[Any, ...]] = []

    def _get_build_plan_description(self) -> PlanDescription | None:
        # Which prior annotations take part in the query, and whether a grouped derived table is
        # needed, follows from the plan key - the steps of the build that recorded values are kept
        # with the plan (result_reading), and a later query lists its values by them.
        return self._get_query_plan_description(
            AggregateQuery,
            self._metric_keys,
            self._group_bys,
            self._is_none,
            # The keyset boundary compares in the ordering's directions (.before_cursor()
            # reverses them).
            tuple(self._orderings),
            describes_cursor=True,
        )

    def _get_plan_values(self, description: PlanDescription, plan: StatementPlan) -> list[Any]:
        if self._bound_rows_query is not None:
            return description.values
        return self._get_recorded_step_values(plan.result_reading)

    def _get_plan_value_count(self, description: PlanDescription) -> int:
        if self._bound_rows_query is not None:
            return len(description.values)
        return len(self._get_recorded_step_values(tuple(self._recorded_steps)))

    def _get_plan_record(self) -> dict[str, Any]:
        if self._bound_rows_query is not None:
            return {}
        return {"result_reading": tuple(self._recorded_steps)}

    def _build_statement(self, value_wrapper_refs: RecordedValueRefs | None, *, records_for_caller: bool) -> bool:
        if self._bound_rows_query is not None:
            return self._build_statement_over_rows(value_wrapper_refs)
        self.query = self._get_base_query()
        self._apply_effective_basetable()

        # Only the metrics are selected - a non-aggregate annotation next to a bare aggregate would
        # need a GROUP BY. No automatic GROUP BY: aggregate() gives one row.
        keeps_plan = True
        recorded_steps: list[tuple[Any, ...]] = []
        self._recorded_steps = recorded_steps
        prior_annotation_keys = [key for key in self._annotations if key not in self._metric_keys]
        if self._group_bys:
            if not self._make_query_over_group_by_subquery(value_wrapper_refs, recorded_steps):
                keeps_plan = False
        elif prior_annotation_keys or self._distinct or self._distinct_on:
            self._make_query_from_referenced_annotations(value_wrapper_refs, recorded_steps)
        else:
            self._apply_filters_and_check_aggregates(
                self._metric_keys, value_wrapper_refs=value_wrapper_refs, recorded_steps=recorded_steps
            )
            self._apply_recorded_ctes(value_wrapper_refs, recorded_steps)
        if self._is_none:
            # A .none() aggregate still runs, with an always-false condition - the database gives
            # each expression its value for an empty set.
            self.query = self.query.where(self._get_never_true_criterion())
        return keeps_plan

    def _get_recorded_step_values(self, recorded_steps: tuple[tuple[Any, ...], ...]) -> list[Any]:
        """This query's values, in the order the build steps recorded their references.

        Args:
            recorded_steps: The steps (``RecordedBuildStep``) - a ``FILTERS`` step with the keys of
                the annotations it resolved and whether it read the filters.

        Returns:
            The values.
        """
        values: list[Any] = []
        context = PlanContext(self._annotations) if self._annotations else PlanContext.EMPTY
        for step in recorded_steps:
            if step[0] == RecordedBuildStep.CTES:
                values.extend(cast("PlanDescription", self._get_ctes_plan_description()).values)
                continue
            _step, annotation_keys, reads_filters = step
            # One expression under several keys is resolved once (_get_annotate()).
            seen_annotation_ids: set[int] = set()
            for key in annotation_keys:
                annotation = self._annotations[key]
                if id(annotation) not in seen_annotation_ids and isinstance(annotation, Plannable):
                    seen_annotation_ids.add(id(annotation))
                    values.extend(cast("PlanDescription", annotation.get_plan_description(context)).values)
            if reads_filters:
                values.extend(
                    cast("PlanDescription", self._get_conditions_plan_description(self._q_objects, context)).values
                )
            # The keyset boundary is applied by an ordered query only (_get_cursor_criterion()).
            if getattr(self, "_orderings", None):
                values.extend(self._get_cursor_plan_description().values)
        return values

    def _apply_recorded_ctes(
        self, value_wrapper_refs: RecordedValueRefs | None, recorded_steps: list[tuple[Any, ...]]
    ) -> None:
        """Applies the CTEs, recording their values.

        Args:
            value_wrapper_refs: The references being recorded, None when the query keeps no plan.
            recorded_steps: The build steps recorded so far.
        """
        self._apply_with_ctes(value_wrapper_refs=value_wrapper_refs)
        recorded_steps.append((RecordedBuildStep.CTES,))

    def _get_recorded_filters(
        self,
        fields_for_select: Collection[str],
        value_wrapper_refs: RecordedValueRefs | None,
        recorded_steps: list[tuple[Any, ...]],
    ) -> None:
        """Builds the filters (``get_filters()``), recording the step: which annotations it
        resolves - neither an ``.alias()`` nothing reads nor a grouped derived table's column,
        which holds no values - and whether it reads the filters.

        Args:
            fields_for_select: The annotation keys to SELECT.
            value_wrapper_refs: The references being recorded, None when the query keeps no plan.
            recorded_steps: The build steps recorded so far.
        """
        if value_wrapper_refs is not None:
            unused_alias_keys = self._get_unused_alias_keys(fields_for_select)
            recorded_steps.append(
                (
                    RecordedBuildStep.FILTERS,
                    tuple(
                        key
                        for key, annotation in self._annotations.items()
                        if key not in unused_alias_keys and not isinstance(annotation, self.GroupedSubqueryColumn)
                    ),
                    bool(self._q_objects),
                )
            )
        self.get_filters(fields_for_select, value_wrapper_refs=value_wrapper_refs)

    def _apply_filters_and_check_aggregates(
        self,
        fields_for_select: Collection[str],
        *,
        value_wrapper_refs: RecordedValueRefs | None = None,
        recorded_steps: list[tuple[Any, ...]] | None = None,
    ) -> None:
        """Builds the SELECT/JOIN/WHERE/HAVING parts of the query, then rejects a SELECTed
        aggregate that nests another aggregate function inside itself.

        Args:
            fields_for_select: The annotation keys to SELECT.
            value_wrapper_refs: The references being recorded, None when the query keeps no plan.
            recorded_steps: The build steps recorded so far.

        Raises:
            QueryError: A selected expression is an aggregate over an aggregate or a window
                function.
        """
        self._get_recorded_filters(
            fields_for_select, value_wrapper_refs, recorded_steps if recorded_steps is not None else []
        )
        for select_term in self.query._selects:
            nodes: Iterator[Any] = select_term.nodes_()
            for node in nodes:
                if (
                    isinstance(node, AggregateFunction)
                    and not node.is_analytic
                    and any(self._term_reads_window_function(argument) for argument in node.args)
                ):
                    raise QueryError(
                        f"'{select_term.alias}' is an aggregate function over a window function "
                        "(Window(...)) annotation - SQL cannot aggregate a window function in the query "
                        "computing it. Fetch the windowed values with .values_list(...) and aggregate them "
                        "in Python instead."
                    )
                if isinstance(node, AggregateFunction) and any(
                    self._contains_aggregate_function(argument) for argument in node.args
                ):
                    raise QueryError(
                        f"'{select_term.alias}' is an aggregate function over another aggregate "
                        "(for example Sum('n') where the annotation 'n' is itself a Count(...)) - SQL "
                        "cannot nest them. Compute the outer one in .aggregate(), e.g. "
                        ".annotate(n=Count(...)).aggregate(total=Sum('n'))."
                    )

    @staticmethod
    def _contains_aggregate_function(term: Term) -> bool:
        """Whether `term` or anything nested in it is an aggregate function call."""
        nodes: Iterator[Any] = term.nodes_()
        return any(isinstance(node, AggregateFunction) for node in nodes)

    @staticmethod
    def _get_never_true_criterion() -> BasicCriterion:
        """SQL has no literal FALSE, so `1 = 0` stands in for it."""
        return BasicCriterion(
            Equality.EQ,
            ValueWrapper(1, allow_parametrize=False),
            ValueWrapper(0, allow_parametrize=False),
        )

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
            The referenced keys, in `annotations` order.
        """
        tracker = self.AnnotationAccessTracker(annotations)
        for expression in root_expressions:
            if isinstance(expression, Expression):
                expression.get_result(self._get_probe_expression_context(tracker))
        for q_object in q_objects:
            q_object.get_result(self._get_probe_expression_context(tracker))
        return [key for key in annotations if key in tracker.accessed_keys]

    def _annotation_is_aggregate(self, annotation: Expression | Term) -> bool:
        """Whether `annotation` resolves to an aggregate term (`Count(...)`, `Coalesce(Sum(...), 0)`,
        an arithmetic expression over one, ...)."""
        if isinstance(annotation, Term) and not isinstance(annotation, Expression):
            return annotation.contains_aggregate
        return annotation.get_result(self._get_probe_expression_context(self._annotations)).term.contains_aggregate

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
        self, value_wrapper_refs: RecordedValueRefs | None, recorded_steps: list[tuple[Any, ...]]
    ) -> None:
        """Builds the query of a queryset carrying annotations or ``.distinct()``. With no referenced
        aggregate annotation and no ``.distinct()`` it is a plain one-row aggregate; otherwise the
        rows are first reduced to one per base row in a grouped derived table.

        Args:
            value_wrapper_refs: The references being recorded, None when the query keeps no plan.
            recorded_steps: The build steps recorded so far.

        Raises:
            QueryError: The queryset is ``.distinct(<fields>)``.
        """
        if self._distinct_on:
            raise QueryError(
                "aggregate() on a .distinct(<fields>) queryset is not supported - its result would be "
                "silently wrong. Use a plain .distinct() or aggregate over a subquery instead."
            )
        annotations = self._annotations
        metric_annotations = [annotation for key, annotation in annotations.items() if key in self._metric_keys]
        referenced_keys = self._get_referenced_annotation_keys(annotations, metric_annotations, self._q_objects)
        aggregate_keys = [key for key in referenced_keys if self._annotation_is_aggregate(annotations[key])]
        try:
            if aggregate_keys or self._distinct:
                self._make_query_over_grouped_subquery(
                    referenced_keys, aggregate_keys, value_wrapper_refs, recorded_steps
                )
            else:
                self._annotations = {
                    key: annotation
                    for key, annotation in annotations.items()
                    if key in self._metric_keys or key in referenced_keys
                }
                self._apply_filters_and_check_aggregates(
                    self._metric_keys, value_wrapper_refs=value_wrapper_refs, recorded_steps=recorded_steps
                )
                self._apply_recorded_ctes(value_wrapper_refs, recorded_steps)
        finally:
            self._annotations = annotations

    def _make_query_over_grouped_subquery(
        self,
        referenced_keys: list[str],
        aggregate_keys: list[str],
        value_wrapper_refs: RecordedValueRefs | None,
        recorded_steps: list[tuple[Any, ...]],
    ) -> None:
        """Computes the metrics over the base table joined to a derived table carrying the referenced
        aggregate annotations - one row per base row passing the filters.

        Args:
            referenced_keys: Every prior annotation the metrics or the filters read.
            aggregate_keys: Those of them that are aggregates.
            value_wrapper_refs: The references being recorded, None when the query keeps no plan.
            recorded_steps: The build steps recorded so far.
        """
        annotations = self._annotations
        effective_table = self._effective_basetable()
        primary_key_columns = [
            self.model._meta.fields_db_projection[attr_name] for attr_name in self.model._meta.pk_attr_names
        ]

        self._annotations = {key: annotation for key, annotation in annotations.items() if key in referenced_keys}
        self._apply_filters_and_check_aggregates(
            aggregate_keys, value_wrapper_refs=value_wrapper_refs, recorded_steps=recorded_steps
        )
        for index, column in enumerate(primary_key_columns):
            self.query = self.query.select(
                effective_table[column].as_(f"{AGGREGATE_SUBQUERY_PRIMARY_KEY_ALIAS_PREFIX}{index}")
            )
        self.query = self.query.groupby(*(effective_table[column] for column in primary_key_columns))
        grouped_subquery = self.query.as_(AGGREGATE_SUBQUERY_ALIAS)

        self._reset_joined_tables()
        self.query = self._get_base_query()
        self._apply_effective_basetable()
        join_criterion = None
        for index, column in enumerate(primary_key_columns):
            column_criterion = effective_table[column].eq(
                grouped_subquery[f"{AGGREGATE_SUBQUERY_PRIMARY_KEY_ALIAS_PREFIX}{index}"]
            )
            join_criterion = column_criterion if join_criterion is None else join_criterion & column_criterion
        self.query = self.query.join(grouped_subquery).on(join_criterion)

        outer_annotations = {
            key: self.GroupedSubqueryColumn(
                grouped_subquery[key], self._get_annotation_value_field(annotation, annotations)
            )
            if key in aggregate_keys
            else annotation
            for key, annotation in annotations.items()
        }
        metric_annotations = [annotation for key, annotation in annotations.items() if key in self._metric_keys]
        outer_referenced_keys = self._get_referenced_annotation_keys(outer_annotations, metric_annotations, ())
        original_q_objects = self._q_objects
        self._q_objects = []
        self._annotations = {
            key: annotation
            for key, annotation in outer_annotations.items()
            if key in self._metric_keys or key in aggregate_keys or key in outer_referenced_keys
        }
        try:
            self._get_recorded_filters(self._metric_keys, value_wrapper_refs, recorded_steps)
        finally:
            self._q_objects = original_q_objects
            self._annotations = annotations
        self._apply_recorded_ctes(value_wrapper_refs, recorded_steps)

    def _make_query_over_group_by_subquery(
        self, value_wrapper_refs: RecordedValueRefs | None, recorded_steps: list[tuple[Any, ...]]
    ) -> bool:
        """Computes the metrics over the rows of a ``.group_by()`` queryset - a derived table with
        one row per group, carrying its group-by columns and the aggregate annotations the metrics
        read, like ``.group_by(...).values(...)`` returns them.

        Args:
            value_wrapper_refs: The references being recorded, None when the query keeps no plan.
            recorded_steps: The build steps recorded so far.

        Returns:
            Whether the query can keep a plan - the metrics over the derived table are resolved
            without recording values, so not when one holds a value of its own.

        Raises:
            QueryError: The queryset is ``.distinct(<fields>)``.
            QueryError: A metric reads a column that isn't grouped by, or a relation.
        """
        if self._distinct_on:
            raise QueryError(
                "aggregate() on a .distinct(<fields>) queryset is not supported - its result would be "
                "silently wrong. Use a plain .distinct() or aggregate over a subquery instead."
            )
        annotations = self._annotations
        grouped_annotation_names = [name for name in self._group_bys if name in annotations]
        metric_annotations = [annotation for key, annotation in annotations.items() if key in self._metric_keys]
        referenced_keys = self._get_referenced_annotation_keys(
            annotations,
            [*metric_annotations, *(annotations[name] for name in grouped_annotation_names)],
            self._q_objects,
        )
        referenced_keys = [key for key in annotations if key in referenced_keys or key in grouped_annotation_names]
        aggregate_keys = [key for key in referenced_keys if self._annotation_is_aggregate(annotations[key])]
        grouped_subquery_columns: dict[str, tuple[str, Field[Any] | None]] = {}
        try:
            self._annotations = {key: annotation for key, annotation in annotations.items() if key in referenced_keys}
            self._apply_filters_and_check_aggregates(
                [*aggregate_keys, *grouped_annotation_names],
                value_wrapper_refs=value_wrapper_refs,
                recorded_steps=recorded_steps,
            )
            for key in (*aggregate_keys, *grouped_annotation_names):
                grouped_subquery_columns[key] = (key, self._get_annotation_value_field(annotations[key], annotations))
            group_by_terms = self._get_group_bys(*self._group_bys)
            for index, (field_name, group_by_term) in enumerate(zip(self._group_bys, group_by_terms, strict=True)):
                if field_name in annotations:
                    continue
                column_alias = f"{AGGREGATE_SUBQUERY_GROUP_BY_ALIAS_PREFIX}{index}"
                self.query = self.query.select(group_by_term.as_(column_alias))
                grouped_subquery_columns[field_name] = (column_alias, self._get_field_object_by_path(field_name))
            self.query._groupbys = group_by_terms
        finally:
            self._annotations = annotations
        grouped_subquery = self.query.as_(AGGREGATE_SUBQUERY_ALIAS)

        self._reset_joined_tables()
        self.query = self._db.query_class.from_(grouped_subquery)
        outer_annotations = {
            **annotations,
            **{
                name: self.GroupedSubqueryColumn(grouped_subquery[column_alias], output_field)
                for name, (column_alias, output_field) in grouped_subquery_columns.items()
            },
        }
        outer_expression_context = self._get_probe_expression_context(outer_annotations)
        for key, annotation in annotations.items():
            if key not in self._metric_keys:
                continue
            if isinstance(annotation, Term) and not isinstance(annotation, Expression):
                term = annotation
            else:
                result = annotation.get_result(outer_expression_context)
                term = result.term
                self._raise_if_metric_reads_ungrouped_column(key, result.joins, term, grouped_subquery)
                if getattr(annotation, "populate_field_object", False):
                    self._annotation_output_fields[key] = annotation.get_value_field(result)
            self.query._select_other(term.as_(key))  # type:ignore[arg-type]
        self._apply_recorded_ctes(value_wrapper_refs, recorded_steps)
        return self._get_rows_summary_structure() is not None

    def _get_rows_summary_structure(self) -> tuple[Any, ...] | None:
        """Each metric's name and structure - a metric over the rows is resolved without recording
        values, so one holding a value keeps no plan."""
        structures = []
        for key in self._metric_keys:
            annotation = self._annotations[key]
            if not isinstance(annotation, Expression):
                return None
            description = annotation.get_plan_description(PlanContext.EMPTY)
            if description is None or description.values:
                return None
            structures.append((key, description.structure))
        return tuple(structures)

    def _get_query_over_rows(self, built_rows_query: AwaitableQuery[Any]) -> QueryBuilder:
        """Computes the metrics over the rows of a ``.values()``/``.values_list()`` query (or set
        operation) as a derived table - a metric reads a selected column by its output name, or by
        the field name it selects when that name isn't an output name too.

        Args:
            built_rows_query: The query, built.

        Returns:
            The query computing the metrics.

        Raises:
            QueryError: A metric reads a column that isn't selected, or a relation.
        """
        output_columns = built_rows_query._get_output_columns()  # type: ignore[attr-defined]
        inner_query = copy(built_rows_query.query)
        with_clauses, inner_query._with = inner_query._with, []
        rows_subquery = inner_query.as_(AGGREGATE_SUBQUERY_ALIAS)
        column_annotations: dict[str, Any] = {}
        for output_name, _selected_name, alias, output_field in output_columns:
            column_annotations[output_name] = self.GroupedSubqueryColumn(rows_subquery[alias], output_field)
        for _output_name, selected_name, alias, output_field in output_columns:
            if selected_name not in column_annotations:
                column_annotations[selected_name] = self.GroupedSubqueryColumn(rows_subquery[alias], output_field)
        self._reset_joined_tables()
        query = self._db.query_class.from_(rows_subquery)
        # An annotation of the rows the query doesn't select (a flat values_list().annotate())
        # resolves over the base table, which the check below rejects as an unselected column.
        unselected_annotations = {
            name: annotation
            for name, annotation in getattr(self._rows_query, "_annotations", {}).items()
            if name not in column_annotations
        }
        outer_expression_context = self._get_probe_expression_context(
            {**unselected_annotations, **self._annotations, **column_annotations}
        )
        for key, annotation in self._annotations.items():
            if key not in self._metric_keys:
                continue
            if isinstance(annotation, Term) and not isinstance(annotation, Expression):
                term = annotation
            else:
                result = annotation.get_result(outer_expression_context)
                term = result.term
                self._raise_if_metric_reads_unselected_column(key, result.joins, term, rows_subquery)
                if getattr(annotation, "populate_field_object", False):
                    self._annotation_output_fields[key] = annotation.get_value_field(result)
            query._select_other(term.as_(key))  # type:ignore[arg-type]
        query._with = with_clauses
        if self._is_none:
            query = query.where(self._get_never_true_criterion())
        return query

    @staticmethod
    def _raise_if_metric_reads_unselected_column(key: str, joins: list[Any], term: Term, rows_subquery: Any) -> None:
        """Rejects a metric over a ``.values()``/``.values_list()`` query's rows that reads
        anything but its selected columns.

        Args:
            key: The metric's name.
            joins: The joins resolving the metric asked for.
            term: The resolved metric.
            rows_subquery: The derived table of the rows.

        Raises:
            QueryError: The metric reads a relation or a column that isn't selected.
        """
        nodes: Iterator[Any] = term.nodes_()
        if joins or any(isinstance(node, SqlField) and node.table is not rows_subquery for node in nodes):
            raise QueryError(
                f"aggregate() metric {key!r} over a .values()/.values_list() query reads a column it doesn't "
                "select - it runs over the rows the query returns (its groups, distinct rows or slice), so it "
                "can only read the selected columns. Select that column too."
            )

    @staticmethod
    def _raise_if_metric_reads_ungrouped_column(key: str, joins: list[Any], term: Term, grouped_subquery: Any) -> None:
        """Rejects a metric over a ``.group_by()`` queryset that reads anything but the group-by
        columns and the annotations.

        Args:
            key: The metric's name.
            joins: The joins resolving the metric asked for.
            term: The resolved metric.
            grouped_subquery: The derived table of the grouped rows.

        Raises:
            QueryError: The metric reads a relation or a column that isn't grouped by.
        """
        nodes: Iterator[Any] = term.nodes_()
        if joins or any(isinstance(node, SqlField) and node.table is not grouped_subquery for node in nodes):
            raise QueryError(
                f"aggregate() metric {key!r} over a .group_by() queryset reads a column that isn't grouped "
                "by - it runs over the grouped rows, so it can only read the .group_by() fields and the "
                "annotations. Group by that column too, or annotate it with an aggregate first."
            )

    def __await__(self) -> Generator[Any, None, dict[str, Any]]:
        query = self._get_execution_query()
        query._make_query_to_run()
        return query._execute_with_retry_context(query._execute()).__await__()

    async def _execute(self) -> dict[str, Any]:
        result = await self._db.execute_dicts(*self._get_parameterized_sql())
        row = result[0] if result else dict.fromkeys(self._metric_keys)
        for key in self._metric_keys:
            # Mirrors ValuesQuery.get_value_reader()'s identical read of this same
            # per-query-execution dict, populated by _get_annotate() only for the specific
            # functions that opt in (e.g. Max/Min - result type equals the source field's type).
            field_object = self._annotation_output_fields.get(key)
            if field_object:
                row[key] = self.dialect.types.get_python_value(field_object, row[key])
        # In the order the metrics were passed, like Django - self._metric_keys is a set.
        ordered_metric_keys = [key for key in self._annotations if key in self._metric_keys]
        ordered_metric_keys += sorted(self._metric_keys.difference(ordered_metric_keys))
        return {key: row[key] for key in ordered_metric_keys}
