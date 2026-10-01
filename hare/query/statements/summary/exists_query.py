from __future__ import annotations

from collections.abc import Generator
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.summary.rows_summary_query import RowsSummaryQuery
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.queries.builder.query_builder import QueryBuilder


class ExistsQuery(RowsSummaryQuery):
    #: A plain SELECT 1 ... LIMIT 1 - see AwaitableQuery.is_read_only. Inherited by ContainsQuery
    #: below, which only adds a WHERE criterion on top of the same read-only query.
    is_read_only: ClassVar[bool] = True

    #: Only bool(result) is read, never an aggregate's value - two aggregated to-many relations in
    #: one query are fine here.
    aggregate_value_is_unused: ClassVar[bool] = True

    def _keeps_plan_built_into_another(self) -> bool:
        # Built into Exists(...), it records its filter and annotation values in the order
        # Exists.get_plan_description() lists them - that description decides the outer plan.
        return True

    def _get_build_plan_description(self) -> PlanDescription | None:
        # type(self) is ExistsQuery, not isinstance() - ContainsQuery appends its own per-object
        # primary key criterion after the build, outside any plan, so it keeps none.
        if type(self) is not ExistsQuery:
            return None
        description = self._get_query_plan_description(
            ExistsQuery,
            # The keyset boundary compares in the ordering's directions; an OFFSET is bound
            # after every other value.
            tuple(self._orderings),
            bool(self._offset),
            describes_cursor=True,
        )
        if description is not None and self._offset:
            description = PlanDescription(description.structure, [*description.values, self._offset])
        return description

    def _build_statement(self, value_wrapper_refs: RecordedValueRefs | None, *, records_for_caller: bool) -> bool:
        if self._bound_rows_query is not None:
            return self._build_statement_over_rows(value_wrapper_refs)
        self.query = self._get_base_query()
        self._apply_effective_basetable()
        self.get_filters(value_wrapper_refs=value_wrapper_refs)
        self._apply_auto_group_by()
        self._apply_with_ctes(value_wrapper_refs=value_wrapper_refs)
        self.query._limit = self.query._wrapper_cls(1)
        if self._offset:
            self.query._offset = self.query._wrapper_cls(self._offset)
            if value_wrapper_refs is not None:
                value_wrapper_refs.append((ValueRefOrigin.SUBQUERY, LiteralValueRef(self.query._offset)))
        self._finalize_select_list()
        return True

    applies_offset_over_rows: ClassVar[bool] = True

    def _get_query_over_rows(self, built_rows_query: AwaitableQuery[Any]) -> QueryBuilder:
        query = (
            self._db.query_class.from_(copy(built_rows_query.query))
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
            *(effective_table[self.model._meta.fields_db_projection[attr]] for attr in self.model._meta.pk_attr_names)
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
        result, _ = await self._db.execute(*self._get_parameterized_sql(), returns_rows=True)
        return bool(result)
