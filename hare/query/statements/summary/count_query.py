from __future__ import annotations

from collections.abc import Generator
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.building.query_conditions import QueryConditions
from hare.query.statements.building.query_ctes import QueryCtes
from hare.query.statements.building.query_grouping import QueryGrouping
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.summary.rows_summary_query import RowsSummaryQuery
from hare.sql.analytics.count import Count
from hare.sql.functions.count import Count as DistinctCount
from hare.sql.terms.star import Star

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.builder.queries.query_builder import QueryBuilder


class CountQuery(RowsSummaryQuery):
    #: A plain SELECT COUNT(*) - see AwaitableQuery.is_read_only.
    is_read_only: ClassVar[bool] = True

    #: The keyset boundary compares in the ordering's directions.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_orderings", PlanKeyForm.TUPLE),
        *StatementPlanDescriptions.ROWS_SLOTS,
        *StatementPlanDescriptions.CURSOR_SLOTS,
    )

    def _get_build_plan_description(self) -> PlanDescription | None:
        return self._describe_statement(False)

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        if self._bound_rows_query is not None:
            # COUNT(*) ignores the OFFSET - _execute() applies it.
            return self._build_statement_over_rows(value_wrapper_references)
        self.query = self._get_base_query()
        QueryJoins.apply_effective_basetable(self)
        QueryConditions.get_filters(self, value_wrapper_references=value_wrapper_references)
        QueryGrouping.apply_auto_group_by(self)
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

    def _group_by_primary_key(self) -> None:
        """Groups by the base table's primary key - one group per row, so the rows an aggregate's JOIN
        multiplies are counted once.
        """
        effective_table = self._effective_basetable()
        self.query = self.query.groupby(
            *(
                effective_table[self.model._meta.fields_db_projection[attribute_name]]
                for attribute_name in self.model._meta.primary_key_attribute_names
            )
        )

    def _get_distinct_count_term(self) -> Any:
        """``COUNT(DISTINCT <pk>)`` for a ``.distinct()`` queryset - the rows a JOIN multiplies are
        counted once. Only where a row's identity is its primary key; anything else raises.
        """
        if self._distinct_on or self.model._meta.has_composite_primary_key:
            raise QueryError(
                "count() on a .distinct() queryset is only supported for a single-column primary key "
                "with no .distinct(<fields>) - its result would be silently wrong."
            )
        return DistinctCount(self._effective_basetable()[self.model._meta.db_pk_column]).distinct()

    def __await__(self) -> Generator[Any, None, int]:
        if self._is_none:
            return self._execute_none().__await__()
        query = self._get_execution_query()
        query._make_query_to_run()
        return query._execute_with_retry_context(query._execute()).__await__()

    async def _execute_none(self) -> int:
        return 0

    async def _execute(self) -> int:
        connection = self._connection
        by_position = connection.features.supports_positional_rows
        _, result = await connection.execute(
            *self._get_parameterized_sql(), returns_rows=True, rows_by_position=by_position
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
