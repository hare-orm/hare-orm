from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.building.query_ctes import QueryCtes
from hare.query.statements.constants import COUNT_SUBQUERY_VALUE_ALIAS
from hare.query.statements.summary.rows_summary_query import RowsSummaryQuery
from hare.sql.analytics.count import Count
from hare.sql.enums import AnalyticFunctionName
from hare.sql.functions.coalesce import Coalesce
from hare.sql.functions.count import Count as DistinctCount
from hare.sql.terms.case.case import Case
from hare.sql.terms.functions.aggregate_function import AggregateFunction
from hare.sql.terms.star import Star
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model_binding import ModelBinding
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.terms.term import Term


class CountQuery(RowsSummaryQuery):
    """``queryset.count()`` - the number of the queryset's rows."""

    rows_source_method_name = "_get_count_source"

    #: A plain SELECT COUNT(*) - see AwaitableQuery.is_read_only.
    is_read_only: ClassVar[bool] = True

    #: The keyset boundary compares in the ordering's directions.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_orderings", PlanKeyForm.TUPLE),
        *StatementPlanDescriptions.ROWS_SLOTS,
        *StatementPlanDescriptions.CURSOR_SLOTS,
    )

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        if self._bound_rows_query is not None:
            # COUNT(*) ignores the OFFSET - _execute() applies it.
            return self._build_statement_over_rows(value_wrapper_references)
        self._build_rows_selection(value_wrapper_references)
        if self._has_aggregate and not self._distinct and not self.query._groupbys:
            self._group_by_primary_key()
        QueryCtes.apply_with_ctes(self, value_wrapper_references=value_wrapper_references)
        count_term = Count(Star())
        if self._distinct and (self._distinct_on or not self.query._groupbys):
            count_term = self._get_distinct_count_term()
        elif self.query._groupbys:
            # One row per group - an implicit GROUP BY always includes the primary key, so its
            # groups are already the distinct rows a .distinct() asks for.
            count_term = count_term.over()

        # Remove annotations from the SELECT list - COUNT(*)/COUNT(*) OVER() doesn't need them
        # rendered as their own output columns.
        self.query._selects = []
        self.query._select_other(count_term)
        return True

    def _get_query_over_rows(
        self, built_rows_query: AwaitableQuery[Any], value_wrapper_references: RecordedValueReferences | None
    ) -> QueryBuilder:
        return self._connection.query_class.from_(copy(built_rows_query.query)).select(Count(Star()))

    def _make_subquery(self, *, value_wrapper_references: RecordedValueReferences | None = None) -> None:
        """Build one scalar count, applying the slice in SQL instead of the terminal reader.

        Grouped counts repeat their total on every surviving group; MAX reads it once and
        COALESCE supplies zero when no group survives. The outer selection always returns one
        row, including for an empty source. Standalone count execution keeps its ordinary reader.
        """
        super()._make_subquery(value_wrapper_references=value_wrapper_references)
        rows = self.query
        rows._selects = [rows._selects[0].as_(COUNT_SUBQUERY_VALUE_ALIAS)]
        count: Term = Coalesce(
            AggregateFunction(AnalyticFunctionName.MAX, rows[COUNT_SUBQUERY_VALUE_ALIAS]),
            ValueWrapper(0, allow_parametrize=False),
        )
        if self._offset:
            offset = ValueWrapper(self._offset)
            count = Case().when(count < offset, 0).else_(count - offset)
        if self._limit is not None:
            limit = ValueWrapper(self._limit)
            count = Case().when(count > limit, limit).else_(count)
        self.query = self._connection.query_class.from_(rows).select(count)

    def _get_distinct_count_term(self) -> Any:
        """``COUNT(DISTINCT <pk>)`` for a ``.distinct()`` queryset - the rows a JOIN multiplies are
        counted once. Only where a row's identity is its primary key; anything else raises.
        """
        meta: ModelBinding = self._binding  # type: ignore[assignment]
        if self._distinct_on or meta.has_composite_primary_key or not meta.has_primary_key:
            raise QueryError(
                "count() on a .distinct() queryset is only supported for a single-column primary key "
                "with no .distinct(<fields>) - its result would be silently wrong."
            )
        return DistinctCount(self._effective_basetable()[meta.db_pk_column]).distinct()

    async def _execute_bound(self) -> int:
        if self._is_none:
            return 0
        return await self._execute_scalar_summary()

    async def _read_scalar_summary(self, connection: DatabaseClient, sql: str, parameters: list[Any]) -> int:
        """Runs the ``COUNT`` and applies the slice to it.

        Args:
            connection: This execution's connection.
            sql: The statement.
            parameters: Its parameters.

        Returns:
            The number of rows.
        """
        by_position = connection.features.supports_positional_rows
        _, result = await connection._execute_built(
            sql, parameters, returns_rows=True, rows_by_position=by_position, retry_read=True
        )
        if not result:
            return 0
        # The one column - by position where the rows are read so, else the one value of the row.
        row_count = result[0][0] if by_position else next(iter(result[0].values()))
        # COUNT(*) ignores LIMIT/OFFSET, so the offset is applied here. Clamp at
        # 0: when the offset is past the total, SQL would return 0 rows, not a
        # negative count.
        count = max(0, row_count - self._offset)
        # Use ``is not None`` so an explicit ``limit(0)`` clamps to 0 instead of
        # being treated as "no limit" by a truthiness check.
        if self._limit is not None and count > self._limit:
            return self._limit
        return count
