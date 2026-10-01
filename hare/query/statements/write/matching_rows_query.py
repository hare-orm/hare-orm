from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.dialects.identifiers import Identifiers
from hare.query.composite import KeyColumns
from hare.query.expressions import ExpressionContext
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.exists_term import ExistsTerm
from hare.query.expressions.subquery import Subquery
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.statement_plans import StatementPlans
from hare.query.queryset.query_spec import QuerySpec
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.star import Star
from hare.sql.terms.tuple import Tuple

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.plans.statement_plan import StatementPlan
    from hare.query.queryset.queryset import QuerySet


class MatchingRowsQuery(AwaitableQuery[Any]):
    """Shared base of ``UpdateQuery``/``DeleteQuery`` - both write the rows the originating
    queryset's filters, and any slice of it, select."""

    # Never built into another query - it keeps plans of its own only.
    plannable = False

    __slots__ = ("_limit_term",)

    def __init__(self, source: QuerySpec[Any]) -> None:
        """Takes the rows ``source`` matches - its filters, annotations, slice, the ordering the
        slice is taken in and ``.distinct()`` - and the connection the write runs on.

        Args:
            source: The queryset written, or a query made from one.
        """
        self.take_rows_of(source, keeps_ordering=True)
        self._init_build_state()
        # The LIMIT the build put in the statement (or its matching-rows subquery).
        self._limit_term: ValueWrapper | None = None

    def _matching_queryset(self) -> QuerySet[Any]:
        """A plain QuerySet, past the default manager, with this query's filters, slice, ordering and
        ``.distinct()``.

        Returns:
            The queryset returning exactly the rows this query writes.
        """
        from hare.query.queryset.queryset import QuerySet

        check_query: QuerySet[Any] = QuerySet(self.model)
        QuerySpec.copy_spec(self, check_query)
        return check_query

    def _get_plan(self) -> tuple[PlanDescription | None, StatementPlan | None]:
        # _get_write_plan_description() decides whether the statement keeps a plan.
        description = self._get_build_plan_description()
        if description is None:
            return None, None
        return description, StatementPlans.find(description.structure)

    def _get_write_plan_description(self, *parts: Any) -> PlanDescription | None:
        """Describes this statement for its plan: the structure, and the values picking the written
        rows in the order their references are recorded - annotations, filters, keyset boundary,
        LIMIT - or those of the primary key subquery picking them.

        Args:
            parts: The structure only this type of statement has.

        Returns:
            The description, None for a statement that keeps no plan.
        """
        rows_values: list[Any] | None = None
        if self._needs_primary_key_subquery_for_rows():
            rows_description = self._get_matching_primary_key_subquery().query.get_plan_description(PlanContext.EMPTY)
            if rows_description is None:
                return None
            parts = (*parts, rows_description.structure)
            rows_values = rows_description.values
        if not self._query_state_is_plannable():
            return None
        query_description = self._get_query_plan_description(
            type(self),
            *parts,
            tuple(self._orderings),
            # Whether the rows are sliced - the LIMIT is bound per query; an OFFSET or a
            # .distinct(<fields>) picks the rows by a subquery.
            self._limit is not None,
            bool(self._offset),
            describes_cursor=True,
        )
        if query_description is None:
            return None
        if rows_values is None:
            rows_values = self._get_matching_rows_values()
        return PlanDescription(query_description.structure, rows_values)

    def _get_matching_rows_values(self) -> list[Any]:
        """The values the rows this statement writes are picked by, in the order their references
        are recorded: the annotations', the filters', the keyset boundary's, then the LIMIT.

        Returns:
            The values.
        """
        values = cast("PlanDescription", self._get_filters_plan_description()).values
        values = values + self._get_cursor_plan_description().values
        if self._limit is not None:
            values.append(self._limit)
        return values

    def _get_ctes_values(self) -> list[Any]:
        """The values of the CTEs of a statement that keeps a plan.

        Returns:
            The values.
        """
        return cast("PlanDescription", self._get_ctes_plan_description()).values

    def _record_matching_rows_limit(self, value_wrapper_refs: RecordedValueRefs | None) -> None:
        """Records where the LIMIT sits, after the filters' references - the statement's own or
        its matching-rows subquery's.

        Args:
            value_wrapper_refs: The references being recorded, None when the statement keeps no
                plan.
        """
        limit_term = self._limit_term
        if value_wrapper_refs is not None and limit_term is not None:
            value_wrapper_refs.append((ValueRefOrigin.SUBQUERY, LiteralValueRef(limit_term)))

    def _needs_primary_key_subquery_for_rows(self) -> bool:
        """Whether the written rows are picked by a ``pk IN (SELECT ...)`` subquery of the full
        matching SELECT - an OFFSET, a LIMIT the dialect's UPDATE/DELETE doesn't take, or
        ``.distinct(<fields>)``.
        """
        return (
            bool(self._offset)
            or (self._limit is not None and not self.features.supports_update_limit_order_by)
            or bool(self._distinct_on)
        )

    def _filters_need_primary_key_subquery(self) -> bool:
        """Whether the built filters can't be attached to the UPDATE/DELETE itself - they join
        another table, or filter on an aggregate (a GROUP BY with HAVING, which neither statement
        accepts, and which dropped would write every row).

        Returns:
            True when the rows have to be picked by a ``pk IN (SELECT ...)`` subquery.
        """
        return bool(self._joined_tables or self.query._havings or self.query._groupbys)

    def _get_matching_rows_criterion(self, matching_query: QueryBuilder) -> Criterion:
        """Picks the rows a SELECT over the model's table matches, for an UPDATE/DELETE that can't take
        its joins or grouping: ``pk IN (SELECT pk ...)`` - for a model without a primary key,
        ``EXISTS`` over a second reference to the table matched by every column.

        Args:
            matching_query: The SELECT picking the rows, with nothing selected yet.

        Returns:
            The criterion over the base table.
        """
        meta = self.model._meta
        table = meta.basetable
        if meta.has_primary_key:
            pk_fields = [table[column] for column in KeyColumns.get_source_columns(meta)]
            pk_reference = pk_fields[0] if len(pk_fields) == 1 else Tuple(*pk_fields)
            return pk_reference.isin(matching_query.select(*pk_fields))
        inner_table = table.as_(Identifiers.get_within_limit(f"{table.get_table_name()}__matching"))
        inner_query = matching_query.replace_table(table, inner_table).select(Star())
        return ExistsTerm(inner_query.where(KeyColumns.get_row_correlation(meta, inner_table, table)))

    def _get_matching_primary_key_subquery(self) -> Subquery:
        """The subquery of the primary keys of the rows this statement writes - the same SELECT
        the matching queryset runs, its ordering, slice, ``.distinct()`` and implicit GROUP BY
        included.

        Returns:
            The subquery, not built yet.
        """
        self.model._meta.raise_if_no_primary_key(f"{type(self).__name__} over an ordered, sliced or distinct queryset")
        matching_queryset = self._matching_queryset()
        # The enclosing statement carries the WITH clause - the subquery only references it.
        matching_queryset._with_ctes = []
        return Subquery(matching_queryset._get_primary_key_values_query())

    def _get_matching_primary_key_criterion(self, value_wrapper_refs: RecordedValueRefs | None) -> Term:
        """``pk IN (<primary keys of the matching rows>)``.

        Args:
            value_wrapper_refs: Where the subquery records its value references, None when the
                statement keeps no plan.

        Returns:
            The criterion over the base table.
        """
        subquery = self._get_matching_primary_key_subquery()
        table = self.model._meta.basetable
        if value_wrapper_refs is not None:
            subquery.get_result(
                ExpressionContext(
                    model=self.model,
                    dialect=self.dialect,
                    connection=self._db,
                    table=table,
                    annotations={},
                    value_wrapper_refs=value_wrapper_refs,
                )
            )
        pk_fields = [
            table[self.model._meta.fields_map[name].source_field or name] for name in self.model._meta.pk_attr_names
        ]
        pk_reference = pk_fields[0] if len(pk_fields) == 1 else Tuple(*pk_fields)
        return pk_reference.isin(subquery)
