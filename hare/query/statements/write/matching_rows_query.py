from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import QueryError
from hare.models.write.rules.written_target_checks import WrittenTargetChecks
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.key_columns import KeyColumns
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.pending_calls.pending_filter_calls import PendingFilterCalls
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.identifiers import Identifiers
from hare.sql.order import Order
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.star import Star
from hare.sql.terms.subqueries.exists_term import ExistsTerm
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.instrumentation.capture.captured_write import CapturedWrite
    from hare.query.plans.statement.statement_plan import StatementPlan
    from hare.query.queryset.queryset import QuerySet


class MatchingRowsQuery(AwaitableQuery[Any]):
    """Shared base of ``UpdateQuery``/``DeleteQuery`` - both write the rows the originating
    queryset's filters, and a slice of it where the dialect's statement takes a LIMIT, select."""

    #: The queryset method writing the rows, for a message.
    written_method_name: ClassVar[str]

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

    def _restrict_to_keys(self, pks: list[Any]) -> None:
        """Narrows the write to the rows of some primary keys - the rows read before it, so a row
        committed by another transaction in between is left as it is.

        Args:
            pks: The primary keys.
        """
        self._filter_call_counter += 1
        generation = self._filter_call_counter
        PendingFilterCalls.add_filter_conditions(
            self,
            False,
            PendingFilterCalls.get_filter_kwarg_conditions(self, {"pk__in": pks}, generation),
            generation,
        )

    async def _run_captured_by_reading(
        self, captured: CapturedWrite, run_statement: Callable[[], Awaitable[int | None]]
    ) -> tuple[int | None, list[Any]]:
        """Runs the write of a model with ``Meta.change_capture`` on a database whose writes return no
        rows: the matched rows are read first, locked, the write narrowed to them, and an update's rows
        read again after it; their changes go to the model's sink.

        Args:
            captured: The capture of the write - an update or a delete - on this query's connection.
            run_statement: Builds and runs the write, giving how many rows it wrote.

        Returns:
            How many rows the write wrote, and the primary keys of the rows read.
        """
        # Local imports: the queryset package imports the query statements.
        from hare.query.queryset.selection.statement_selection import StatementSelection

        read_before = await captured.read(
            StatementSelection.get_primary_key_values_query(self._matching_queryset()), lock=True
        )
        if not read_before:
            return 0, []
        pks = list(read_before)
        moved_keys = self._get_moved_keys(pks)
        self._restrict_to_keys(pks)
        count = await run_statement()
        if moved_keys:
            # The rows are read again by the keys the write gave them.
            read_before = {moved_keys[pk]: values for pk, values in read_before.items()}
            pks = [moved_keys[pk] for pk in pks]
        await captured.capture_read(read_before, pks)
        return count, pks

    def _get_moved_keys(self, keys: list[Any]) -> dict[Any, Any]:
        """The primary key each row of ``keys`` has after the write - for a write moving rows to other keys.

        Args:
            keys: The keys of the rows written.

        Returns:
            The key after the write by the key before it; empty for a write keeping the keys.
        """
        return {}

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

        # The rows are read by a query of their own here - its methods changing how rows are read
        # aren't refused by the write's own statement.
        WrittenTargetChecks.raise_if_refused_calls(self)
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
        self._raise_if_rows_unwritable()
        description = self._describe_statement(False)
        if description is None or not StatementPlanDescriptions.query_state_is_plannable(self):
            return None
        return description

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

    def _get_slice_orderings(self) -> list[Any]:
        """The orderings a sliced UPDATE/DELETE picks its rows by - the queryset's own, else the
        primary key's: without one the slice is whichever rows the database reads first, which a
        write applied part by part of the table reads again per part and matches none of.

        Returns:
            The orderings.
        """
        if self._orderings or not self.model._meta.has_primary_key:
            return list(self._orderings)
        return [(name, Order.ASC) for name in self.model._meta.primary_key_attribute_names]

    def _get_written_method_name(self) -> str:
        """The queryset method writing the rows, for a message."""
        return self.written_method_name

    def _raise_if_rows_unwritable(self) -> None:
        """Refuses a write of the rows of a slice the dialect's UPDATE/DELETE takes no LIMIT of, of an
        OFFSET, or of ``.distinct(<fields>)`` - as Django refuses them.

        Raises:
            QueryError: The rows are such rows.
        """
        if self._offset or (self._limit is not None and not self.features.supports_update_limit_order_by):
            raise QueryError(f"Cannot {self._get_written_method_name()}() a query once a slice has been taken.")
        if self._distinct_on:
            raise QueryError(f"Cannot call {self._get_written_method_name()}() after .distinct(*fields).")

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
