from __future__ import annotations

from collections.abc import Generator
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.description.plan_context import PlanContext
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
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.builder.queries.query_builder import QueryBuilder


class ExistsQuery(RowsSummaryQuery):
    #: A plain SELECT 1 ... LIMIT 1 - see AwaitableQuery.is_read_only. Inherited by ContainsQuery
    #: below, which only adds a WHERE criterion on top of the same read-only query.
    is_read_only: ClassVar[bool] = True

    #: Only bool(result) is read, never an aggregate's value - two aggregated to-many relations in
    #: one query are fine here.
    aggregate_value_is_unused: ClassVar[bool] = True

    # Built into Exists(...) with its values among the enclosing query's (get_plan_description()).
    plannable = True

    #: The keyset boundary compares in the ordering's directions; an OFFSET is bound.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_orderings", PlanKeyForm.TUPLE),
        ("_offset", PlanKeyForm.BOUND_WHEN_TRUE),
        ("_is_none", PlanKeyForm.VALUE),
        *StatementPlanDescriptions.ROWS_SLOTS,
        *StatementPlanDescriptions.CURSOR_SLOTS,
    )

    def _keeps_plan_built_into_another(self) -> bool:
        # Built into Exists(...), it records its filter and annotation values in the order
        # Exists.get_plan_description() lists them - that description decides the outer plan.
        return True

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Describes this query built into ``Exists(...)`` as its own plan does - over a rows query
        by that query's description and the OFFSET applied over it.

        Args:
            context: The enclosing query's.

        Returns:
            The description, None for a query that keeps no plan.
        """
        if self._rows_query is not None:
            return self._describe_over_rows(True)
        if not StatementPlanDescriptions.query_state_is_plannable(self):
            return None
        return self._describe_statement(True)

    def _get_build_plan_description(self) -> PlanDescription | None:
        return self._describe_statement(False)

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        if self._bound_rows_query is not None:
            return self._build_statement_over_rows(value_wrapper_references)
        self.query = self._get_base_query()
        QueryJoins.apply_effective_basetable(self)
        QueryConditions.get_filters(self, value_wrapper_references=value_wrapper_references)
        QueryGrouping.apply_auto_group_by(self)
        QueryCtes.apply_with_ctes(self, value_wrapper_references=value_wrapper_references)
        self.query._limit = self.query._wrapper_class(1)
        if self._offset:
            self.query._offset = self.query._wrapper_class(self._offset)
            if value_wrapper_references is not None:
                ExpressionArguments.record_literal(value_wrapper_references, self, "_offset", self.query._offset)
        self._finalize_select_list()
        return True

    applies_offset_over_rows: ClassVar[bool] = True

    def _get_query_over_rows(
        self, built_rows_query: AwaitableQuery[Any], value_wrapper_references: RecordedValueReferences | None
    ) -> QueryBuilder:
        query = (
            self._connection.query_class.from_(copy(built_rows_query.query))
            .select(ValueWrapper(1, allow_parametrize=False))
            .limit(1)
        )
        return query.offset(self._offset) if self._offset else query

    def _finalize_select_list(self) -> None:
        """Drops every `.annotate()`-added SELECT column - `exists()` never reads an annotated
        aggregate's own value, only whether the query produced any row at all - then adds the
        literal presence marker."""
        if self._offset and (self._distinct or self._distinct_on):
            self._group_by_primary_key_for_distinct()
        self.query._selects = []
        self.query._select_other(ValueWrapper(1, allow_parametrize=False))  # type:ignore[arg-type]

    def _group_by_primary_key_for_distinct(self) -> None:
        """Groups a sliced ``.distinct()`` query by its primary key, so the offset skips distinct
        rows rather than JOIN-multiplied ones. A GROUP BY rather than ``SELECT DISTINCT``, which
        SQLite drops inside an ``EXISTS (...)`` subquery.

        Raises:
            QueryError: If the queryset uses ``.distinct(<fields>)``.
        """
        if self._distinct_on:
            raise QueryError(
                "exists() on a sliced .distinct() queryset is only supported with no "
                ".distinct(<fields>) - its result would be silently wrong."
            )
        if self.query._groupbys:
            return
        effective_table = self._effective_basetable()
        self.query = self.query.groupby(
            *(
                effective_table[self.model._meta.fields_db_projection[attribute_name]]
                for attribute_name in self.model._meta.primary_key_attribute_names
            )
        )

    def __await__(self) -> Generator[Any, None, bool]:
        if self._is_none or self._limit == 0:
            return self._execute_none().__await__()
        query = self._get_execution_query()
        query._make_query_to_run()
        return query._execute_with_retry_context(query._execute()).__await__()

    async def _execute_none(self) -> bool:
        return False

    async def _execute(
        self,
    ) -> bool:
        result, _ = await self._connection.execute(*self._get_parameterized_sql(), returns_rows=True)
        return bool(result)
