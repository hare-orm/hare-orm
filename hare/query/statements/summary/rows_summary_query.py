from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.query.expressions.subqueries.outer_query_state import outer_expression_context
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.statements.awaitable_query import AwaitableQuery

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.builder.queries.query_builder import QueryBuilder


class RowsSummaryQuery(AwaitableQuery[Any]):
    """Shared base of ``ExistsQuery`` / ``CountQuery`` / ``AggregateQuery`` - one value computed over
    the rows the originating queryset selects, or over a rows query (a ``.values()`` query, a set
    operation, or the queryset's own rows when they repeat) selected from as a derived table.
    """

    # Never built into another query - it keeps plans of its own only.
    plannable = False

    __slots__ = ("_rows_query", "_bound_rows_query")

    def __init__(self, source: QuerySpecification[Any], *, rows_query: AwaitableQuery[Any] | None = None) -> None:
        """Takes the rows ``source`` matches - its filters, annotations, slice and ``.distinct()``.

        Args:
            source: The queryset summarized, or a query made from one.
            rows_query: The rows the value is computed over, as a derived table - a ``.values()``
                query, a set operation, or the queryset's own rows when they repeat. The summary
                then has no filters of its own: only the source's slice, connection and scope.
        """
        if rows_query is None:
            self._take_rows_of(source, keeps_grouping=self.keeps_grouping)
        else:
            QuerySpecification.__init__(self, source.model)
            self._apply_connection(source._connection)
            self._connection_explicitly_chosen = source._connection_explicitly_chosen
            self._router_fallback_connection = source._router_fallback_connection
            self._instance_connection_alias = source._instance_connection_alias
            self._limit = source._limit
            self._offset = source._offset
            self._is_none = source._is_none
            source._share_default_scope(self)
            # The rows query applies the calls of the dialect's QuerySet methods - the summary reads
            # its rows as they are.
            self._options = self._options.updated(extension_calls=())
        self._init_build_state()
        self._rows_query = rows_query
        # The rows query on this query's connection, for one build.
        self._bound_rows_query: AwaitableQuery[Any] | None = None
        self._offset = self._offset or 0

    #: Whether the summary runs over the groups of a ``.group_by()`` queryset.
    keeps_grouping: ClassVar[bool] = False

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

    def _get_rows_query_plan_description(self) -> PlanDescription | None:
        """Describes the rows query the summary is computed over.

        Returns:
            The description, None when it keeps no plan.
        """
        rows_query = self._bound_rows_query if self._bound_rows_query is not None else self._rows_query
        return cast("AwaitableQuery[Any]", rows_query).get_plan_description(PlanContext.EMPTY)

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
        raise NotImplementedError()  # pragma: nocoverage

    def _get_rows_summary_description(self) -> PlanDescription | None:
        """What else of this summary is part of its plan over the rows query - bound after the rows
        query's values.

        Returns:
            The description, or None for a summary that keeps no plan.
        """
        return PlanDescription.EMPTY

    def _prepare_build(self) -> None:
        rows_query = self._rows_query
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
        if outer_expression_context.get() is not None:
            return None, None
        description = self._describe_over_rows(False)
        if description is None:
            return None, None
        return description, StatementPlans.find(description.structure)

    def _describe_over_rows(self, built_into_another: bool) -> PlanDescription | None:
        """Describes the statement over a rows query from ``rows_plan_slots`` (``QueryKeyCompiler``).

        Args:
            built_into_another: Whether the query is built into another one.

        Returns:
            The description, None for a query that keeps no plan.
        """
        raise NotImplementedError  # pragma: nocoverage - generated from rows_plan_slots

    def _build_statement_over_rows(self, value_wrapper_references: RecordedValueReferences | None) -> bool:
        """Builds this summary over its rows query, selected from as a derived table.

        Args:
            value_wrapper_references: The list the rows query's value references are recorded into.

        Returns:
            True - the statement can be kept as a plan.
        """
        rows_query = cast("AwaitableQuery[Any]", self._bound_rows_query)
        if value_wrapper_references is not None:
            rows_query._make_subquery(value_wrapper_references=value_wrapper_references)
        else:
            rows_query._make_subquery()
        self.query = self._get_query_over_rows(rows_query, value_wrapper_references)
        if value_wrapper_references is not None and self.query._offset is not None:
            ExpressionArguments.record_literal(value_wrapper_references, self, "_offset_over_rows", self.query._offset)
        return True
