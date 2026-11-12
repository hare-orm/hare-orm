from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.query.expressions.subqueries.outer_scope import OuterScope
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.building.query_conditions import QueryConditions
from hare.query.statements.building.query_grouping import QueryGrouping
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.rows_source_query import RowsSourceQuery
from hare.query.statements.summary.summary_source import SummarySource

if TYPE_CHECKING:
    from hare.query.queryset.queryset import QuerySet
    from hare.sql.builder.queries.query_builder import QueryBuilder


class RowsSummaryQuery(RowsSourceQuery):
    """Shared base of ``ExistsQuery`` / ``CountQuery`` / ``AggregateQuery`` - one value computed over
    the rows the originating queryset selects, or over a rows query (a ``.values()`` query, a set
    operation, or the queryset's own rows when they repeat) selected from as a derived table.
    """

    # Never built into another query - it keeps plans of its own only.
    plannable = False

    __slots__ = ("_rows_query", "_bound_rows_query", "_extension_source", "_selected_rows", "_selected_rows_arguments")

    def __init__(
        self,
        source: QuerySpecification[Any],
        *,
        rows_query: AwaitableQuery[Any] | None = None,
        selected_rows: QuerySet[Any] | None = None,
        selected_rows_arguments: dict[str, Any] | None = None,
    ) -> None:
        """Takes the rows ``source`` matches - its filters, annotations, slice and ``.distinct()``.

        Args:
            source: The queryset summarized, or a query made from one.
            rows_query: The rows the value is computed over, as a derived table - a ``.values()``
                query, a set operation, or the queryset's own rows when they repeat. The summary
                then has no filters of its own: only the source's slice, connection and scope.
            selected_rows: A ``.values()`` or set-operation queryset whose rows the value is computed
                over - the summary is made of its rows the first time it is built
                (``_make_selected_rows_summary()``), not now.
            selected_rows_arguments: The keywords the summary of ``selected_rows`` is made with.
        """
        self._initialize_source(source, rows_query=rows_query, selected_rows=selected_rows)
        self._selected_rows_arguments = selected_rows_arguments

    @classmethod
    def from_source(cls, description: SummarySource, **arguments: Any) -> Self:
        """Construct one executor from its source's row-selection policy."""
        summary = cls(description.source, rows_query=description.rows_query, **arguments)
        if description.keeps_calls:
            description.source._hand_call_signature(summary)  # type: ignore[attr-defined]
        return summary

    def _initialize_source(
        self,
        source: QuerySpecification[Any],
        *,
        rows_query: AwaitableQuery[Any] | None = None,
        selected_rows: QuerySet[Any] | None = None,
    ) -> None:
        """Initialize row state, also when a deferred source becomes concrete."""
        if rows_query is None and selected_rows is None:
            super().__init__(source, keeps_grouping=self.keeps_grouping)
        else:
            QuerySpecification.__init__(self, source.model)
            self._apply_connection(source._connection)
            self._connection_explicitly_chosen = source._connection_explicitly_chosen
            self._router_fallback_connection = source._router_fallback_connection
            self._instance_connection_alias = source._instance_connection_alias
            self._limit = source._limit
            self._offset = source._offset
            self._is_none = source._is_none
            self._execution_dependencies = source._execution_dependencies
            source._share_default_scope(self)
            # The rows query applies the calls of the dialect's QuerySet methods - the summary reads
            # its rows as they are.
            self._options = self._options.updated(extension_calls=())
            self._init_build_state()
        self._rows_query = rows_query
        self._selected_rows = selected_rows
        self._selected_rows_arguments = None
        self._extension_source = (
            source if rows_query is None and selected_rows is None and source._extension_calls else None
        )
        # The rows query on this query's connection, for one build.
        self._bound_rows_query: AwaitableQuery[Any] | None = None
        self._offset = self._offset or 0

    #: Whether the summary runs over the groups of a ``.group_by()`` queryset.
    keeps_grouping: ClassVar[bool] = False

    #: The method of a rows source (``StatementSelection.get_rows_source()``) describing this summary's rows.
    rows_source_method_name: ClassVar[str]

    #: Whether the query's own OFFSET is applied in SQL over the rows query.
    applies_offset_over_rows: ClassVar[bool] = False

    #: The statement over a rows query: the rows query's structure, the OFFSET applied over it and
    #: what else of the summary is part of its plan - their values bound.
    rows_plan_slots: ClassVar[DeclaredPlanSlots] = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_get_rows_query_plan_description", PlanKeyForm.DESCRIBED),
        ("_offset_over_rows", PlanKeyForm.BOUND_WHEN_TRUE),
        ("_is_none", PlanKeyForm.VALUE),
        ("_get_rows_summary_description", PlanKeyForm.DESCRIBED),
    )

    @property
    def _offset_over_rows(self) -> int | None:
        """The OFFSET applied in SQL over the rows query, None when the summary applies none."""
        return self._offset if self.applies_offset_over_rows else None

    def _make_selected_rows_summary(self) -> None:
        """Makes the summary of the selected rows it stands for, the first time it is built - the summary
        selected by their source, without constructing another summary."""
        selected_rows = self._selected_rows
        if selected_rows is None:
            return
        # Deferred import: the selection module imports the statements.
        from hare.query.queryset.selection.statement_selection import StatementSelection

        rows_source = StatementSelection.get_rows_source(selected_rows)
        arguments = self._selected_rows_arguments or {}
        description = getattr(rows_source, self.rows_source_method_name)()
        self._initialize_source(description.source, rows_query=description.rows_query)
        self._apply_summary_arguments(arguments)
        if description.keeps_calls:
            description.source._hand_call_signature(self)

    def _apply_summary_arguments(self, arguments: dict[str, Any]) -> None:
        """Apply operation-specific declarations after a deferred row source is chosen."""

    def __copy__(self) -> Self:
        # Every build works on a copy - an execution's, or one bound into an outer query: the summary is
        # made before the first one, and each copy takes it.
        self._make_selected_rows_summary()
        return super().__copy__()

    def _get_build_plan_description(self) -> PlanDescription | None:
        return self._describe_statement(False) if self._bound_rows_query is None else self._describe_over_rows(False)

    def _build_rows_selection(self, value_wrapper_references: RecordedValueReferences | None) -> None:
        """Starts the statement over the rows the queryset selects - its table and joins, its filters,
        and the ``GROUP BY`` its aggregate annotations need.

        Args:
            value_wrapper_references: Where the values of the filters are recorded - None when they
                aren't.
        """
        self.query = self._get_base_query()
        QueryJoins.apply_effective_basetable(self)
        QueryConditions.get_filters(self, value_wrapper_references=value_wrapper_references)
        QueryGrouping.apply_auto_group_by(self)

    def _get_rows_query_plan_description(self) -> PlanDescription | None:
        """Describes the rows query the summary is computed over.

        Returns:
            The description, None when it keeps no plan.
        """
        rows_query = self._bound_rows_query if self._bound_rows_query is not None else self._rows_query
        return rows_query.get_plan_description(PlanContext.EMPTY)  # type: ignore[union-attr]

    def _get_query_over_rows(
        self, built_rows_query: AwaitableQuery[Any], value_wrapper_references: RecordedValueReferences | None
    ) -> QueryBuilder:
        """The query selecting this summary from the rows query.

        Args:
            built_rows_query: The rows query, built - select from a copy of its ``query``, since
                selecting from it sets its alias and it can be the query a plan keeps.
            value_wrapper_references: The list the summary's own value references are recorded into, in
                the order ``_get_rows_summary_description()`` lists the values - None when no plan
                is recorded.

        Returns:
            The query.
        """
        raise NotImplementedError()

    def _get_rows_summary_description(self) -> PlanDescription | None:
        """What else of this summary is part of its plan over the rows query - bound after the rows
        query's values.

        Returns:
            The description, or None for a summary that keeps no plan.
        """
        return PlanDescription.EMPTY

    def _prepare_build(self) -> None:
        rows_query = self._rows_query
        if rows_query is None and self._extension_source is not None and QuerySetExtensions.changes_rows(self):
            # Source selection is delayed until this execution has its connection.
            from hare.query.queryset.selection.statement_selection import StatementSelection

            source = self._extension_source._clone()  # type: ignore[attr-defined]
            source._apply_connection(self._connection)
            if not self.keeps_grouping:
                source = StatementSelection.get_values_rows_queryset(source)
                source._limit = source._offset = None
            rows_query = StatementSelection.get_values_query(source.values())
            self._rows_query = rows_query
            self._extension_calls = ()
            self._bound_extensions = ()
        if rows_query is not None:
            rows_query = copy(rows_query)
            rows_query._apply_connection(self._connection)
        self._bound_rows_query = rows_query

    def _get_plan(self) -> tuple[PlanDescription | None, StatementPlan | None]:
        """Over a rows query the key holds its structure (its ``get_plan_description()``); the
        values are its values and the OFFSET applied over it."""
        if self._bound_rows_query is None:
            return super()._get_plan()
        # Inside a correlated subquery the plan also depends on the enclosing query.
        if OuterScope.current_expression_context.get() is not None:
            return None, None
        description = self._get_build_plan_description()
        if description is None:
            return None, None
        return description, StatementPlans.find(self.model, description.structure)

    def _describe_over_rows(self, built_into_another: bool) -> PlanDescription | None:
        """Describes the statement over a rows query from ``rows_plan_slots`` (``QueryKeyCompiler``).

        Args:
            built_into_another: Whether the query is built into another one.

        Returns:
            The description, None for a query that keeps no plan.
        """
        raise NotImplementedError

    def _build_statement_over_rows(self, value_wrapper_references: RecordedValueReferences | None) -> bool:
        """Builds this summary over its rows query, selected from as a derived table.

        Args:
            value_wrapper_references: The list the rows query's value references are recorded into.

        Returns:
            True - the statement can be kept as a plan.
        """
        rows_query: AwaitableQuery[Any] = self._bound_rows_query  # type: ignore[assignment]
        if value_wrapper_references is not None:
            rows_query._make_subquery(value_wrapper_references=value_wrapper_references)
        else:
            rows_query._make_subquery()
        self.query = self._get_query_over_rows(rows_query, value_wrapper_references)
        if value_wrapper_references is not None and self.query._offset is not None:
            ExpressionArguments.record_literal(value_wrapper_references, self, "_offset_over_rows", self.query._offset)
        return True
