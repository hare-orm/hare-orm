from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.exceptions import QueryError
from hare.models.write.rules.written_target_checks import WrittenTargetChecks
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.key_columns import KeyColumns
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declarations import CallPlanHit
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plan_runs import StatementPlanRuns
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.statements.building.query_conditions import QueryConditions
from hare.query.statements.building.query_grouping import QueryGrouping
from hare.query.statements.building.query_ordering import QueryOrdering
from hare.query.statements.rows_source_query import RowsSourceQuery
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.identifiers import Identifiers
from hare.sql.order import Order
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.star import Star
from hare.sql.terms.subqueries.exists_term import ExistsTerm
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.capture.captured_write import CapturedWrite
    from hare.query.execution.prepared_write import PreparedWrite
    from hare.query.plans.statement.statement_plan import StatementPlan
    from hare.query.queryset.queryset import QuerySet


class MatchingRowsQuery(RowsSourceQuery):
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
        super().__init__(source, keeps_ordering=True)
        self._call_signature = getattr(source, "_call_signature", None)
        self._call_values = getattr(source, "_call_values", ())
        # The LIMIT the build put in the statement (or its matching-rows subquery).
        self._limit_term: ValueWrapper | None = None

    def _get_statement_on(
        self,
        connection: DatabaseClient,
        source: QuerySet[Any] | None = None,
        *,
        type_part: tuple[Any, Sequence[Any]] = ((), []),
        prepared: PreparedWrite | None = None,
    ) -> CallPlanHit | tuple[Self, tuple[str, list[Any]]]:
        """Find SQL before creating build state; only a miss materializes a writable copy."""
        specification = self if source is None else source
        lookup = StatementPlanRuns.find_call_plan(type(self), specification, connection, type_part)
        if isinstance(lookup, CallPlanHit):
            return lookup
        query = self.__copy__()
        if source is not None:
            query._take_rows_of(source, keeps_ordering=True)
        query._init_build_state()
        query._call_signature = specification._call_signature
        query._call_values = specification._call_values
        query._apply_connection(connection)
        query._is_execution_query = True
        if prepared is not None:
            # UpdateQuery owns assignments; deletes pass no prepared values.
            query._prepared_write = prepared  # type: ignore[attr-defined]
        StatementPlanRuns.prepare_call_plan_record(query, lookup)
        return query, query._make_query_to_run()

    def _build_matching_rows(self, value_wrapper_references: RecordedValueReferences | None) -> None:
        """Build the SELECT whose filters and slice select the rows to write.

        UPDATE and DELETE turn this selection into their statement directly or match its
        primary keys through a subquery. Filter aggregates receive their required grouping.

        Args:
            value_wrapper_references: The bound values recorded for a statement plan, if any.
        """
        self.query = self._get_base_query()
        if self._limit is not None:
            self.query._limit = self._limit_term = self.query._wrapper_class(self._limit)
            QueryOrdering.get_ordering(
                self, self.model, self.model._meta.basetable, self._get_slice_orderings(), self._annotations
            )

        QueryConditions.get_filters(self, value_wrapper_references=value_wrapper_references)
        self._record_matching_rows_limit(value_wrapper_references)
        # An aggregate annotation in the filters needs its GROUP BY, or the HAVING aggregates over
        # the whole joined result.
        QueryGrouping.apply_auto_group_by(self)

    async def _run_captured_by_reading(
        self,
        captured: CapturedWrite,
        run_statement: Callable[[QuerySet[Any]], Awaitable[int | None]],
        *,
        get_moved_keys: Callable[[list[Any]], dict[Any, Any]] | None = None,
    ) -> tuple[int | None, list[Any]]:
        """Runs the write of a model with ``Meta.change_capture`` on a database whose writes return no
        rows: the matched rows are read first, locked, the write narrowed to them, and an update's rows
        read again after it; their changes go to the model's sink.

        Args:
            captured: The capture of the write - an update or a delete - on this query's connection.
            run_statement: Builds and runs the write, giving how many rows it wrote.
            get_moved_keys: Map old keys to written keys using this execution's prepared values.

        Returns:
            How many rows the write wrote, and the primary keys of the rows read.
        """
        # Local imports: the queryset package imports the query statements.
        from hare.query.queryset.selection.statement_selection import StatementSelection

        source = self._matching_queryset(captured.connection)
        read_before = await captured.read(StatementSelection.get_primary_key_values_query(source), lock=True)
        if not read_before:
            return 0, []
        pks = list(read_before)
        moved_keys = {} if get_moved_keys is None else get_moved_keys(pks)
        count = await run_statement(self._filter_matching_rows(source, pk__in=pks))
        if moved_keys:
            # The rows are read again by the keys the write gave them.
            read_before = {moved_keys[pk]: values for pk, values in read_before.items()}
            pks = list(moved_keys.values())
        await captured.capture_read(read_before, pks)
        return count, pks

    def _matching_queryset(self, connection: DatabaseClient | None = None) -> QuerySet[Any]:
        """A plain QuerySet, past the default manager, with this query's filters, slice, ordering and
        ``.distinct()``.

        Returns:
            The queryset returning exactly the rows this query writes.
        """
        from hare.query.queryset.queryset import QuerySet

        # A write has discarded the source's row-return settings. Copy that declaration, not
        # the original source, which can still carry only(), prefetches or grouping settings.
        check_query = QuerySet(self.model)
        SpecificationCopying.copy_specification(self, check_query)
        check_query._call_signature = self._call_signature
        check_query._call_values = self._call_values
        connection = connection if connection is not None else self._connection
        if connection is not None:
            check_query._apply_connection(connection)
            check_query._connection_explicitly_chosen = True
        check_query._visibility = self._visibility.get_for_active_tenant()
        return check_query

    @staticmethod
    def _filter_matching_rows(source: QuerySet[Any], **filters: Any) -> QuerySet[Any]:
        """Narrow a write's rows while retaining its supported LIMIT and ordering.

        Public filtering after a slice is refused. This internal narrowing creates a new
        declaration, adds the predicate, then restores the original write's slice.
        """
        if source._limit is None and not source._offset:
            return source.filter(**filters)
        rows = source._clone_keeping_calls()
        rows._limit = rows._offset = None
        rows = rows.filter(**filters)
        rows._limit, rows._offset = source._limit, source._offset
        return rows

    async def _get_matching_pks(self, connection: DatabaseClient, *, live_only: bool = False) -> list[Any]:
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
        WrittenTargetChecks.raise_if_refused_calls(self, connection.dialect)
        primary_key_attribute_names = self.model._meta.primary_key_attribute_names
        is_single_pk = len(primary_key_attribute_names) == 1
        matching_pks = list(
            dict.fromkeys(await StatementSelection.get_primary_key_values_query(self._matching_queryset(connection)))
        )
        if not live_only or not matching_pks:
            return matching_pks
        from hare.query.queryset.queryset import QuerySet

        soft_delete_field: str = self.model._meta.soft_delete_field
        live_query = QuerySet(self.model).filter(pk__in=matching_pks, **{f"{soft_delete_field}__isnull": True})
        live_query._apply_connection(connection)
        live_query._visibility = replace(
            self._visibility.get_for_active_tenant(), include_deleted=True, only_deleted=False
        )
        live_pks = set(await live_query.values_list(*primary_key_attribute_names, flat=is_single_pk))
        return [pk for pk in matching_pks if pk in live_pks]

    def _get_plan(self) -> tuple[PlanDescription | None, StatementPlan | None]:
        # _get_build_plan_description() decides whether the statement keeps a plan.
        description = self._get_build_plan_description()
        if description is None:
            return None, None
        return description, StatementPlans.find(self.model, description.structure)

    def _get_build_plan_description(self) -> PlanDescription | None:
        self._raise_if_rows_unwritable()
        description = self._describe_statement(False)
        if description is None or not StatementPlanDescriptions.query_state_is_plannable(self):
            return None
        return description

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
        if self._extension_calls:
            WrittenTargetChecks.raise_if_refused_calls(self, self.dialect)
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
