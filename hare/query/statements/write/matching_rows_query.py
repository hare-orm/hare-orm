from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.query.expressions import ExpressionContext
from hare.query.expressions.subqueries.subquery import Subquery
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.key_columns import KeyColumns
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.identifiers import Identifiers
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.star import Star
from hare.sql.terms.subqueries.exists_term import ExistsTerm
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.plans.statement.statement_plan import StatementPlan
    from hare.query.queryset.queryset import QuerySet


class MatchingRowsQuery(AwaitableQuery[Any]):
    """Shared base of ``UpdateQuery``/``DeleteQuery`` - both write the rows the originating
    queryset's filters, and any slice of it, select."""

    # Never built into another query - it keeps plans of its own only.
    plannable = False

    __slots__ = ("_limit_term",)

    #: The statement picking its rows itself: the rows' filters, the ordering the slice is taken
    #: in, the keyset boundary and the LIMIT, bound.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_orderings", PlanKeyForm.TUPLE),
        ("_limit", PlanKeyForm.BOUND),
        *StatementPlanDescriptions.ROWS_SLOTS,
        *StatementPlanDescriptions.CURSOR_SLOTS,
    )
    #: The statement picking its rows by a primary key subquery - the subquery holds the rows'
    #: filters, ordering and slice.
    subquery_plan_slots: ClassVar[DeclaredPlanSlots] = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_get_matching_rows_plan_description", PlanKeyForm.DESCRIBED),
        ("_distinct", PlanKeyForm.VALUE),
        (StatementPlanDescriptions.get_options_structure, PlanKeyForm.METHOD),
        (StatementPlanDescriptions.get_ctes_plan_description, PlanKeyForm.DESCRIBED),
        (StatementPlanDescriptions.get_extension_calls_plan_description, PlanKeyForm.DESCRIBED),
        (StatementPlanDescriptions.get_zone_structure, PlanKeyForm.METHOD),
    )

    def __init__(self, source: QuerySpecification[Any]) -> None:
        """Takes the rows ``source`` matches - its filters, annotations, slice, the ordering the
        slice is taken in and ``.distinct()`` - and the connection the write runs on.

        Args:
            source: The queryset written, or a query made from one.
        """
        self._take_rows_of(source, keeps_ordering=True)
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
        SpecificationCopying.copy_specification(self, check_query)
        return check_query

    async def _get_matching_pks(self, *, live_only: bool = False) -> list[Any]:
        """Primary keys of every row this query's own filters match, each listed once - a
        filter through a to-many relation JOINs, so the matching queryset returns the same parent
        row once per matching child.

        Args:
            live_only: Drop the rows ``Meta.soft_delete_field`` already marks as deleted, after
                the filters (and any slice) picked the matching rows.

        Returns:
            The distinct pks in first-seen order; a tuple per row for a composite primary key.
        """
        # Local import: the queryset package imports the query statements.
        from hare.query.queryset.selection.statement_selection import StatementSelection

        primary_key_attribute_names = self.model._meta.primary_key_attribute_names
        is_single_pk = len(primary_key_attribute_names) == 1
        matching_pks = list(
            dict.fromkeys(await StatementSelection.get_primary_key_values_query(self._matching_queryset()))
        )
        if not live_only or not matching_pks:
            return matching_pks
        from hare.query.queryset.queryset import QuerySet

        soft_delete_field = cast("str", self.model._meta.soft_delete_field)
        live_query = QuerySet(self.model).filter(pk__in=matching_pks, **{f"{soft_delete_field}__isnull": True})
        live_query._apply_connection(self._connection)
        live_query._visibility = replace(self._visibility, include_deleted=True, only_deleted=False)
        live_pks = set(await live_query.values_list(*primary_key_attribute_names, flat=is_single_pk))
        return [pk for pk in matching_pks if pk in live_pks]

    def _get_plan(self) -> tuple[PlanDescription | None, StatementPlan | None]:
        # _get_build_plan_description() decides whether the statement keeps a plan.
        description = self._get_build_plan_description()
        if description is None:
            return None, None
        return description, StatementPlans.find(description.structure)

    def _get_build_plan_description(self) -> PlanDescription | None:
        if self._needs_primary_key_subquery_for_rows():
            description = self._describe_through_subquery(False)
        else:
            description = self._describe_statement(False)
        if description is None or not StatementPlanDescriptions.query_state_is_plannable(self):
            return None
        return description

    def _get_matching_rows_plan_description(self) -> PlanDescription | None:
        """Describes the subquery of the primary keys of the rows the statement writes.

        Returns:
            The description, None when it keeps no plan.
        """
        return self._get_matching_primary_key_subquery().query.get_plan_description(PlanContext.EMPTY)

    def _describe_through_subquery(self, built_into_another: bool) -> PlanDescription | None:
        """Describes the statement picking its rows by a primary key subquery from
        ``subquery_plan_slots`` (``QueryKeyCompiler``).

        Args:
            built_into_another: Whether the query is built into another one.

        Returns:
            The description, None for a statement that keeps no plan.
        """
        raise NotImplementedError  # pragma: nocoverage - generated from subquery_plan_slots

    def _get_ctes_description(self) -> PlanDescription:
        """The values of the CTEs of a statement that keeps a plan.

        Returns:
            The description.
        """
        return StatementPlanDescriptions.get_ctes_plan_description(self)  # type: ignore[return-value]

    def _record_matching_rows_limit(self, value_wrapper_references: RecordedValueReferences | None) -> None:
        """Records where the LIMIT sits, after the filters' references - the statement's own or
        its matching-rows subquery's.

        Args:
            value_wrapper_references: The references being recorded, None when the statement keeps no
                plan.
        """
        limit_term = self._limit_term
        if value_wrapper_references is not None and limit_term is not None:
            ExpressionArguments.record_literal(value_wrapper_references, self, "_limit", limit_term)

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
        # Local import: the queryset package imports the query statements.
        from hare.query.queryset.selection.statement_selection import StatementSelection

        self.model._meta.raise_if_no_primary_key(f"{type(self).__name__} over an ordered, sliced or distinct queryset")
        self._build_conditions_for_copies()
        matching_queryset = self._matching_queryset()
        # The enclosing statement carries the WITH clause - the subquery only references it.
        matching_queryset._with_ctes = []
        primary_key_values_query = StatementSelection.get_primary_key_values_query(matching_queryset)
        # Made again for each description and build - its values come from this statement.
        primary_key_values_query._plan_origin = self
        return Subquery(primary_key_values_query)

    def _get_matching_primary_key_criterion(self, value_wrapper_references: RecordedValueReferences | None) -> Term:
        """``pk IN (<primary keys of the matching rows>)``.

        Args:
            value_wrapper_references: Where the subquery records its value references, None when the
                statement keeps no plan.

        Returns:
            The criterion over the base table.
        """
        subquery = self._get_matching_primary_key_subquery()
        table = self.model._meta.basetable
        if value_wrapper_references is not None:
            subquery.get_result(
                ExpressionContext(
                    model=self.model,
                    dialect=self.dialect,
                    connection=self._connection,
                    table=table,
                    annotations={},
                    value_wrapper_references=value_wrapper_references,
                )
            )
        pk_fields = [
            table[self.model._meta.fields_map[name].source_field or name]
            for name in self.model._meta.primary_key_attribute_names
        ]
        pk_reference = pk_fields[0] if len(pk_fields) == 1 else Tuple(*pk_fields)
        return pk_reference.isin(subquery)
