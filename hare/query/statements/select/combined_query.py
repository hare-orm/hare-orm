from __future__ import annotations

from collections.abc import AsyncGenerator
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import QueryError
from hare.query.constants import GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.single_rows.get_exceptions import GetExceptions
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.rows.model_rows.combined_model_rows import CombinedModelRows
from hare.query.rows.values_rows.combined_values_rows import CombinedValuesRows
from hare.query.statements.constants import VALUES_SET_OPERATION_ALIAS
from hare.query.statements.select.combined.combined_branches import CombinedBranches
from hare.query.statements.select.combined.combined_derived_queries import CombinedDerivedQueries
from hare.query.statements.select.combined.combined_plan_descriptions import CombinedPlanDescriptions
from hare.query.statements.select.rows_query import RowsQuery
from hare.query.statements.select.select_query import SelectQuery
from hare.query.statements.select.values_query import ValuesQuery
from hare.sql import Order
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.builder.queries.set_operation_query import SetOperationQuery
from hare.sql.enums import SetOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.field import Field
    from hare.query.queryset.combination.combination import Combination
    from hare.query.queryset.queryset import QuerySet
    from hare.query.queryset.single_rows.get_exception_argument import GetExceptionArgument
    from hare.query.relation_loading.prefetching.prefetch import Prefetch
    from hare.query.statements.summary.aggregate_query import AggregateQuery
    from hare.query.statements.summary.count_query import CountQuery
    from hare.query.statements.summary.exists_query import ExistsQuery
    from hare.query.statements.write.update_query import UpdateQuery


class CombinedQuery(RowsQuery[Any]):
    """Builds and runs the SQL of a queryset combining querysets - ``union()``, ``intersection()`` and
    ``difference()``. Each branch is built by the query of its own rows; an ordered or sliced
    branch, and a nested set operation, combines as a derived table. The combined rows are selected
    from the set operation as a derived table.
    """

    #: Each branch's structure as it is built in, the operations, the ordering of the combined rows
    #: and their slice, bound.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        ("_combines_values", PlanKeyForm.VALUE),
        *StatementPlanDescriptions.HEAD_SLOTS,
        (CombinedPlanDescriptions.get_branches_plan_description, PlanKeyForm.DESCRIBED),
        ("_set_operations", PlanKeyForm.VALUE),
        ("_orderings", PlanKeyForm.TUPLE),
        ("_limit", PlanKeyForm.BOUND),
        ("_offset", PlanKeyForm.BOUND_WHEN_TRUE),
        ("_is_none", PlanKeyForm.VALUE),
        (StatementPlanDescriptions.get_zone_structure, PlanKeyForm.METHOD),
    )

    def __init__(self, source_queryset: QuerySet[Any, Any]) -> None:
        """Takes the branches, ordering, slice and connection of a queryset combining querysets.

        Args:
            source_queryset: The queryset.

        Raises:
            QueryError: Model instances are combined with ``.values()``/``.values_list()`` rows.
        """
        # Local import: the queryset package imports the query statements.
        from hare.query.queryset.combination.queryset_combination import QuerySetCombination

        SpecificationCopying.copy_specification(source_queryset, self)
        self._init_build_state()
        combination = cast("Combination", source_queryset._combination)
        self._source_queryset = source_queryset
        #: Whether the rows are the values the branches select, not model instances.
        self._combines_values = QuerySetCombination.selects_values(source_queryset)
        self._branches: tuple[SelectQuery[Any] | CombinedQuery, ...] = tuple(
            CombinedBranches.get_branch_query(self, branch) for branch in combination.branches
        )
        # The operation combining each branch after the first with everything before it, in order.
        self._set_operations: tuple[SetOperation, ...] = combination.set_operations
        self._prefetched_relations: tuple[str | Prefetch, ...] = combination.prefetched_relations
        #: How the rows of the last build are read.
        self._rows: CombinedValuesRows | CombinedModelRows = self._get_rows()

    def _get_rows(self) -> CombinedValuesRows | CombinedModelRows:
        """A reader of the combined rows, for one build."""
        if self._combines_values:
            return CombinedValuesRows(cast("ValuesQuery", CombinedBranches.get_first_leaf(self)))
        return CombinedModelRows(self.model, CombinedBranches.get_models(self), self._prefetched_relations)

    # --- the branches ---------------------------------------------------------------------------

    # --- the build ------------------------------------------------------------------------------

    def _prepare_build(self) -> None:
        self._rows = self._get_rows()

    def _keeps_plan_built_into_another(self) -> bool:
        # Every branch records its own values - one keeping no plan records an empty reference.
        return True

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        return self._get_scoped_copy()._describe_statement(True)

    def _get_plan(self) -> tuple[PlanDescription | None, StatementPlan | None]:
        # The branches' descriptions decide whether the statement keeps a plan.
        description = self._describe_statement(False)
        if description is None:
            return None, None
        return description, StatementPlans.find(description.structure)

    def _restore_from_plan(self, plan: StatementPlan) -> None:
        self._rows.restore(plan.result_reading)

    def _get_plan_record(self) -> dict[str, Any]:
        return {"result_reading": self._rows.get_result_reading()}

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        combined_query: QueryBuilder | SetOperationQuery | None = None
        # Every branch's with_cte() CTEs are hoisted to the combined statement as a whole - a
        # WITH clause rendered on a branch would land after UNION/INTERSECT/EXCEPT.
        with_clauses: list[Any] = []
        # name -> (the first branch's CTE, the with_cte() body it was built from)
        with_clauses_by_name: dict[str, tuple[Any, Any]] = {}
        for branch_index, branch in enumerate(self._branches):
            branch_query, cte_bodies_by_name = CombinedBranches.get_branch_statement(
                self, branch, branch_index, value_wrapper_references
            )
            CombinedBranches.hoist_with_clauses(branch_query, cte_bodies_by_name, with_clauses, with_clauses_by_name)
            branch_query = copy(branch_query)
            branch_query._with = []
            branch_query.wrap_set_operation_queries = False
            if combined_query is None:
                combined_query = branch_query
            else:
                combined_query = CombinedBranches.combine(self, combined_query, branch_query, branch_index)
        derived_table = cast("QueryBuilder | SetOperationQuery", combined_query).as_(VALUES_SET_OPERATION_ALIAS)
        rows = self._rows
        query = self._connection.query_class.from_(derived_table).select(
            *(derived_table.field(alias).as_(alias) for alias in rows.get_output_aliases())
        )
        for field_name, order in self._orderings:
            query = query.orderby(derived_table.field(rows.get_ordering_alias(field_name)), order=order)
        if self._limit is not None:
            query._limit = query._wrapper_class(self._limit)
        if self._offset:
            query._offset = query._wrapper_class(self._offset)
        query._with = with_clauses
        self.query = query
        if value_wrapper_references is not None:
            # The slice is among the values - bound per query.
            for attribute, slice_bound_term in (("_limit", query._limit), ("_offset", query._offset)):
                if slice_bound_term is not None:
                    ExpressionArguments.record_literal(value_wrapper_references, self, attribute, slice_bound_term)
        return True

    # --- running --------------------------------------------------------------------------------

    async def _execute(self) -> Any:
        sql, parameters = self._get_parameterized_sql()
        rows = await self._rows.read(self, sql, parameters)
        return self._get_single_or_list_result(rows)

    def _get_default_iteration_orderings(self) -> list[tuple[str, Order]]:
        # Worked out with the tie-breaker - the combined columns are known once the statement is
        # built.
        return []

    def _get_orderings_with_tie_breaker(self) -> list[tuple[str, Order]]:
        """The ordering - unordered, by the primary key for model instances and by every output
        column for values - with every combined column appended as a tie-breaker."""
        built_query = self._get_execution_query()
        built_query._make_query()
        own_orderings = list(self._orderings) or built_query._rows.get_default_orderings()
        ordering_names = {field_name for field_name, _order in own_orderings}
        return [
            *own_orderings,
            *((name, Order.ASC) for name in built_query._rows.get_output_names() if name not in ordering_names),
        ]

    def _stream_batches(self, connection: DatabaseClient, chunk_size: int) -> AsyncGenerator[list[Any]]:
        return cast(
            "AsyncGenerator[list[Any]]",
            self._rows.stream_batches(self, connection, *self._get_parameterized_sql(), chunk_size),
        )

    # --- the rows as a derived table ---------------------------------------------------------------

    def _get_rows_query(self, *, sliced: bool) -> CombinedQuery:
        """A copy to select from as a derived table, returning a list.

        Args:
            sliced: Keep the slice and ordering.

        Returns:
            The copy.
        """
        query = copy(self)
        query._single = False
        query._raise_does_not_exist = False
        query._prefetched_relations = ()
        if not sliced:
            query._limit = None
            query._offset = None
            query._orderings = []
        return query

    def _get_output_columns(self) -> list[tuple[str, str, str, Field[Any] | None]]:
        """The combined columns of the built query, to read from it as a derived table - see
        ``ValuesQuery._get_output_columns()``."""
        return self._rows.get_output_columns()

    def _get_output_names(self) -> list[str]:
        """The output names of the combined values, in output order."""
        return self._rows.get_output_names()

    def _get_count_query(self) -> CountQuery:
        """The number of rows the query returns, counted over the query as a derived table."""
        from hare.query.statements.summary.count_query import CountQuery

        return CountQuery(self, rows_query=self._get_rows_query(sliced=False))

    def _get_exists_query(self) -> ExistsQuery:
        """Whether the query returns any row."""
        from hare.query.statements.summary.exists_query import ExistsQuery

        return ExistsQuery(self, rows_query=self._get_rows_query(sliced=False))

    def _get_aggregate_query(self, **kwargs: Any) -> AggregateQuery:
        """Aggregates over the combined rows - a metric reads the combined columns: the output
        names of values, the fields and annotations of model instances.

        Raises:
            QueryError: A metric reads anything but the combined columns.
        """
        from hare.query.statements.summary.aggregate_query import AggregateQuery

        return AggregateQuery(self, kwargs, rows_query=self._get_rows_query(sliced=True))

    def _get_field_values_query(
        self, field_name: str, value_wrapper_references: RecordedValueReferences | None = None
    ) -> QueryBuilder:
        """One column of combined model instances, as a query to embed - a queryset combining
        querysets passed as an ``__in`` filter value (``filter(pk__in=a.union(b))``).

        Args:
            field_name: The field to select, ``"pk"`` included.
            value_wrapper_references: The list of the enclosing query the value references go into.

        Returns:
            The query selecting that column from the combined rows.

        Raises:
            QueryError: The model has a composite primary key.
            FieldError: The combined rows don't select the field.
        """
        primary_key_attribute = self.model._meta.primary_key_attribute
        if field_name == "pk":
            self.model._meta.raise_if_no_primary_key("a union used as an __in filter value")
            if isinstance(primary_key_attribute, tuple):
                raise QueryError(
                    f"{self.model.__name__} has a composite primary key - a union can't be used as a "
                    "single-column __in filter value."
                )
            field_name = primary_key_attribute
        return CombinedDerivedQueries.get_fields_values_query(self, (field_name,), value_wrapper_references)

    # --- the queryset's methods -----------------------------------------------------------------

    def _get_update_query(self, **kwargs: Any) -> UpdateQuery:
        """Rejected, like Django.

        Raises:
            QueryError: Always - the combined rows aren't rows of one table.
        """
        raise QueryError(
            "update() can't be used on a union()/intersection()/difference() - call it on each "
            "queryset before combining them."
        )

    def _get_first(self, *, reverse: bool) -> QuerySet[Any, Any]:
        """The first (or last) combined row - in the ordering, else by the primary key for model
        instances and by every output column for values.

        Args:
            reverse: Take the last row.

        Returns:
            The single-row queryset.

        Raises:
            QueryError: ``last()`` of sliced rows.
        """
        if reverse and (self._limit is not None or self._offset):
            raise QueryError("last() can't be used on a sliced set operation - call it before slicing.")
        orderings = list(self._orderings) or self._rows.get_default_orderings()
        queryset = self._source_queryset._clone()
        queryset._orderings = [(name, order.get_reversed()) for name, order in orderings] if reverse else orderings
        queryset._single = True
        queryset._limit = 1 if self._limit is None else min(self._limit, 1)
        return queryset

    def _get_single_queryset(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        does_not_exist_exception: GetExceptionArgument,
        multiple_objects_returned_exception: GetExceptionArgument,
    ) -> QuerySet[Any, Any]:
        """The one combined row. Conditions filter every branch before they are combined - a row
        condition gives the same rows before or after UNION/INTERSECT/EXCEPT.

        Args:
            args: ``Q`` conditions.
            kwargs: Filter keyword arguments.
            does_not_exist_exception: As ``get()`` takes it.
            multiple_objects_returned_exception: As ``get()`` takes it.

        Returns:
            The single-row queryset.

        Raises:
            QueryError: Conditions are given for sliced rows - they depend on the rows the
                conditions drop.
        """
        queryset = self._source_queryset._clone()
        if args or kwargs:
            if self._limit is not None or self._offset:
                raise QueryError(
                    "get() with conditions can't be used on a sliced union - the conditions "
                    "filter the branches, before the slice. Pass them before slicing."
                )
            queryset = CombinedDerivedQueries.get_changed_branches_queryset(
                queryset, lambda branch: branch.filter(*args, **kwargs)
            )
        queryset._single = True
        queryset._raise_does_not_exist = does_not_exist_exception is not None
        GetExceptions.set_get_exceptions(queryset, does_not_exist_exception, multiple_objects_returned_exception)
        fetch_limit = 1 if multiple_objects_returned_exception is None else GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK
        queryset._limit = fetch_limit if self._limit is None else min(self._limit, fetch_limit)
        return queryset
