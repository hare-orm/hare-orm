from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.outer_query_state import outer_expression_context
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.statement_plan import StatementPlan
from hare.query.plans.statement_plans import StatementPlans
from hare.query.queryset.query_spec import QuerySpec
from hare.query.statements.awaitable_query import AwaitableQuery

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.queries.builder.query_builder import QueryBuilder


class RowsSummaryQuery(AwaitableQuery[Any]):
    """Shared base of ``ExistsQuery`` / ``CountQuery`` / ``AggregateQuery`` - one value computed over
    the rows the originating queryset selects, or over a rows query (a ``.values()`` query, a set
    operation, or the queryset's own rows when they repeat) selected from as a derived table.
    """

    # Never built into another query - it keeps plans of its own only.
    plannable = False

    __slots__ = ("_rows_query", "_bound_rows_query")

    def __init__(self, source: QuerySpec[Any], *, rows_query: AwaitableQuery[Any] | None = None) -> None:
        """Takes the rows ``source`` matches - its filters, annotations, slice and ``.distinct()``.

        Args:
            source: The queryset summarized, or a query made from one.
            rows_query: The rows the value is computed over, as a derived table - a ``.values()``
                query, a set operation, or the queryset's own rows when they repeat. The summary
                then has no filters of its own: only the source's slice, connection and scope.
        """
        if rows_query is None:
            self.take_rows_of(source, keeps_grouping=self.keeps_grouping)
        else:
            QuerySpec.__init__(self, source.model)
            self._apply_db(source._db)
            self._db_explicitly_chosen = source._db_explicitly_chosen
            self._router_fallback_db = source._router_fallback_db
            self._instance_connection_name = source._instance_connection_name
            self._limit = source._limit
            self._offset = source._offset
            self._is_none = source._is_none
            source._share_default_scope(self)
        self._init_build_state()
        self._rows_query = rows_query
        # The rows query on this query's connection, for one build.
        self._bound_rows_query: AwaitableQuery[Any] | None = None
        self._offset = self._offset or 0

    #: Whether the summary runs over the groups of a ``.group_by()`` queryset.
    keeps_grouping: ClassVar[bool] = False

    #: Whether the query's own OFFSET is applied in SQL over the rows query.
    applies_offset_over_rows: ClassVar[bool] = False

    def _get_query_over_rows(self, built_rows_query: AwaitableQuery[Any]) -> QueryBuilder:
        """The query selecting this summary from the rows query.

        Args:
            built_rows_query: The rows query, built - select from a copy of its ``query``, since
                selecting from it sets its alias and it can be the query a plan keeps.

        Returns:
            The query.
        """
        raise NotImplementedError()  # pragma: nocoverage

    def _get_rows_summary_structure(self) -> tuple[Any, ...] | None:
        """What else of this summary is part of its plan key over the rows query.

        Returns:
            The structure, or None for a summary that keeps no plan.
        """
        return ()

    def _prepare_build(self) -> None:
        rows_query = self._rows_query
        if rows_query is not None:
            rows_query = copy(rows_query)
            rows_query._apply_db(self._db)
        self._bound_rows_query = rows_query

    def _get_plan(self) -> tuple[PlanDescription | None, StatementPlan | None]:
        """Over a rows query the key holds its structure (its ``get_plan_description()``); the
        values are its values and the OFFSET applied over it."""
        rows_query = self._bound_rows_query
        if rows_query is None:
            return super()._get_plan()
        summary_structure = self._get_rows_summary_structure()
        # Inside a correlated subquery the plan also depends on the enclosing query.
        if outer_expression_context.get() is not None or summary_structure is None:
            return None, None
        rows_description = rows_query.get_plan_description(PlanContext.EMPTY)
        if rows_description is None:
            return None, None
        offset_values = [self._offset] if self.applies_offset_over_rows and self._offset else []
        description = PlanDescription(
            (
                type(self),
                self.model,
                *self._get_connection_structure(True),
                self._get_visibility_structure(),
                rows_description.structure,
                bool(offset_values),
                self._is_none,
                summary_structure,
            ),
            [*rows_description.values, *offset_values],
        )
        return description, StatementPlans.find(description.structure)

    def _build_statement_over_rows(self, value_wrapper_refs: RecordedValueRefs | None) -> bool:
        """Builds this summary over its rows query, selected from as a derived table.

        Args:
            value_wrapper_refs: The list the rows query's value references are recorded into.

        Returns:
            True - the statement can be kept as a plan.
        """
        rows_query = cast("AwaitableQuery[Any]", self._bound_rows_query)
        if value_wrapper_refs is not None:
            rows_query._make_subquery(value_wrapper_refs=value_wrapper_refs)
        else:
            rows_query._make_subquery()
        self.query = self._get_query_over_rows(rows_query)
        if value_wrapper_refs is not None and self.query._offset is not None:
            value_wrapper_refs.append((ValueRefOrigin.SUBQUERY, LiteralValueRef(self.query._offset)))
        return True
