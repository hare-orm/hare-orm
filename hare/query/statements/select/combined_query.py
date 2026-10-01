from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from copy import copy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import FieldError, QueryError
from hare.query.constants import (
    COMBINED_QUERY_APP_COLUMN,
    COMBINED_QUERY_MODEL_COLUMN,
    GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK,
    VALUES_SET_OPERATION_ALIAS,
)
from hare.query.expressions import Ordering
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.raw_sql import RawSQL
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.statement_plan import StatementPlan
from hare.query.plans.statement_plans import StatementPlans
from hare.query.queryset.query_spec import QuerySpec
from hare.query.rows.combined_model_rows import CombinedModelRows
from hare.query.rows.combined_values_rows import CombinedValuesRows
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from hare.query.statements.select.rows_query import RowsQuery
from hare.query.statements.select.select_query import SelectQuery
from hare.query.statements.select.values_query import ValuesQuery
from hare.sql import Order
from hare.sql.enums import SetOperation
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.queries.builder.set_operation_query import SetOperationQuery
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.fields.base.field import Field
    from hare.models import Model
    from hare.query.queryset.combination import Combination
    from hare.query.queryset.queryset import QuerySet
    from hare.query.relation_loading.prefetch import Prefetch
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

    def __init__(self, source_queryset: QuerySet[Any, Any]) -> None:
        """Takes the branches, ordering, slice and connection of a queryset combining querysets.

        Args:
            source_queryset: The queryset.

        Raises:
            QueryError: Model instances are combined with ``.values()``/``.values_list()`` rows.
        """
        QuerySpec.copy_spec(source_queryset, self)
        self._init_build_state()
        combination = cast("Combination", source_queryset._combination)
        self._source_queryset = source_queryset
        #: Whether the rows are the values the branches select, not model instances.
        self._combines_values = source_queryset._selects_values()
        self._branches: tuple[SelectQuery[Any] | CombinedQuery, ...] = tuple(
            self._get_branch_query(branch) for branch in combination.branches
        )
        # The operation combining each branch after the first with everything before it, in order.
        self._set_operations: tuple[SetOperation, ...] = combination.set_operations
        self._prefetched_relations: tuple[str | Prefetch, ...] = combination.prefetched_relations
        #: How the rows of the last build are read.
        self._rows: CombinedValuesRows | CombinedModelRows = self._get_rows()

    def _get_branch_query(self, branch: QuerySet[Any, Any]) -> SelectQuery[Any] | CombinedQuery:
        """The query building a branch."""
        if branch._selects_values() != self._combines_values:
            raise QueryError(
                "Cannot combine model instances with .values()/.values_list() rows - call .values()/"
                ".values_list() on every branch (the rows then take the first branch's shape)."
            )
        if branch._combination is not None:
            return CombinedQuery(branch)
        if branch._direct_get is not None:
            branch._apply_direct_get_filters()
        return branch._get_values_query() if self._combines_values else ModelRowsQuery(branch)

    def _get_first_leaf(self) -> SelectQuery[Any]:
        """The first branch that isn't a set operation itself - the rows take its shape."""
        first_branch = self._branches[0]
        return first_branch._get_first_leaf() if isinstance(first_branch, CombinedQuery) else first_branch

    def _get_models(self) -> set[type[Model]]:
        """The models the combined rows can be instances of."""
        models: set[type[Model]] = set()
        for branch in self._branches:
            models.update(branch._get_models() if isinstance(branch, CombinedQuery) else {branch.model})
        return models

    def _get_rows(self) -> CombinedValuesRows | CombinedModelRows:
        """A reader of the combined rows, for one build."""
        if self._combines_values:
            return CombinedValuesRows(cast("ValuesQuery", self._get_first_leaf()))
        return CombinedModelRows(self.model, self._get_models(), self._prefetched_relations)

    # --- the branches ---------------------------------------------------------------------------

    def _get_prepared_branch(self, branch: SelectQuery[Any] | CombinedQuery) -> SelectQuery[Any] | CombinedQuery:
        """A copy of a branch as it is built into the combined statement: an unsliced branch
        unordered - the order of its rows doesn't change the combined rows - and never ordered by
        ``Meta.ordering``; a branch of model instances tagged with its model.

        Args:
            branch: The branch.

        Returns:
            The copy.

        Raises:
            QueryError: The branch locks its rows, loads relations, or a nested set operation
                prefetches them.
            FieldError: An annotation of a branch of model instances is named after a field.
        """
        prepared_branch = copy(branch)
        if isinstance(prepared_branch, CombinedQuery):
            if prepared_branch._prefetched_relations:
                raise QueryError(
                    "Union queries do not support prefetch_related() on a nested "
                    "union()/intersection()/difference() - apply it to the outer result instead"
                )
            return prepared_branch
        if prepared_branch._select_for_update:
            raise QueryError(
                "select_for_update() can't be used on a branch of union()/intersection()/difference() - SQL "
                "can't lock the rows of a set operation (FOR UPDATE is not allowed with UNION/INTERSECT/"
                "EXCEPT). Lock the rows in a separate query, e.g. "
                "Model.objects.filter(pk__in=...).select_for_update()."
            )
        is_sliced = prepared_branch._limit is not None or bool(prepared_branch._offset)
        if not is_sliced and not prepared_branch._cursor_values and not prepared_branch._before_cursor_values:
            prepared_branch._orderings = []
        prepared_branch._default_ordering_disabled = True
        if isinstance(prepared_branch, ModelRowsQuery):
            self._tag_branch_with_model(prepared_branch)
        return prepared_branch

    def _tag_branch_with_model(self, branch: ModelRowsQuery[Any]) -> None:
        """Checks a branch of model instances can be combined, and selects the two columns naming
        its model.

        Raises:
            QueryError: The branch loads relations - a loaded relation can't survive being merged
                into the combined rows.
            FieldError: An annotation is named after a field of the model - reading the instance
                would overwrite the field's value with the annotation's.
        """
        # A relation's field-level lazy="joined"/"select" default counts like an explicit
        # select_related()/prefetch_related().
        if branch._loads_relations():
            raise QueryError(
                "Union queries do not support select_related()/prefetch_related() on the "
                "individual querysets - the loaded relation cannot survive being merged into "
                "the combined result"
            )
        # An .alias() key is never selected, so it can't collide.
        colliding_keys = branch.model._meta.fields_map.keys() & (branch._annotations.keys() - branch._alias_keys)
        if colliding_keys:
            raise FieldError(
                f"annotate() key(s) {sorted(colliding_keys)} collide with existing field(s) on "
                f"model {branch.model.__name__} - fetching full model instances would silently "
                "corrupt hydration. Use a different annotation name, or .values()/.values_list()."
            )
        # SQL string literals, not bound values: constant for the branch's model, so the plan of
        # the combined statement binds none of them.
        branch._annotations = {
            **branch._annotations,
            COMBINED_QUERY_APP_COLUMN: RawSQL(self._get_string_literal(branch.model._meta.app)),
            COMBINED_QUERY_MODEL_COLUMN: RawSQL(self._get_string_literal(branch.model._meta._model.__name__)),
        }

    @staticmethod
    def _get_string_literal(text: str | None) -> str:
        """An SQL string literal of a text.

        Args:
            text: The text, None for none.

        Returns:
            The literal, its quotes doubled - ``NULL`` for None.
        """
        if text is None:
            return "NULL"
        # Local import: the dialect constants import the query layer through the dialect.
        from hare.dialects.base.constants import SQL_DIALECT

        return SQL_DIALECT.get_string_literal_sql(text)

    def _get_branch_statement(
        self, branch: SelectQuery[Any] | CombinedQuery, branch_index: int, value_wrapper_refs: RecordedValueRefs | None
    ) -> tuple[QueryBuilder, dict[str, Any]]:
        """Builds one branch's SELECT on this query's connection.

        An ordered or sliced branch, and a nested set operation, is a derived table selecting its
        columns - SQL allows ORDER BY/LIMIT only on the whole combined statement.

        Args:
            branch: The branch.
            branch_index: Its position among the branches.
            value_wrapper_refs: The list the branch records its value references into, while the
                plan of the combined statement is recorded.

        Returns:
            The SELECT, and the ``with_cte()`` body of each CTE it carries, by name.

        Raises:
            QueryError: The branch runs on another connection.
        """
        embedded_as = "a branch of union()/intersection()/difference()"
        branch_db = branch._db or branch.get_connection()
        if self._db is not None and branch_db.connection_name != self._db.connection_name:
            # Every branch's rows live on the connection it runs on by itself - its model's own
            # connection, or the router's choice - and the combined statement runs on one.
            raise QueryError(
                f"{branch.model.__name__} query used as {embedded_as} runs on a different database "
                f"connection ({branch_db.connection_name!r}) than the {self.model.__name__} query it is "
                f"combined with ({self._db.connection_name!r}) - the combined statement runs on one "
                "connection. Query each connection separately instead."
            )
        built_branch = self._get_prepared_branch(branch).get_bound_to(self._db, self.model, embedded_as)
        if value_wrapper_refs is not None:
            built_branch._make_subquery(value_wrapper_refs=value_wrapper_refs)
        else:
            built_branch._make_subquery()
        self._rows.take_branch(built_branch, branch_index)
        branch_query = built_branch.query
        if (
            isinstance(built_branch, CombinedQuery)
            or branch_query._orderbys
            or branch_query._limit is not None
            or branch_query._offset is not None
        ):
            inner_query = copy(branch_query)
            with_clauses, inner_query._with = inner_query._with, []
            branch_query = self._db.query_class.from_(inner_query).select(
                *(inner_query.field(alias).as_(alias) for alias in self._rows.get_branch_aliases(built_branch))
            )
            branch_query._with = with_clauses
        return branch_query, dict(built_branch._with_ctes)

    @staticmethod
    def _cte_bodies_match(existing_with_clause: Any, existing_body: Any, with_clause: Any, body: Any) -> bool:
        """Whether two branches' same-named CTEs are one definition - the same ``with_cte()``
        body (each branch compiles it into its own query object), or bodies rendering the same
        SQL with the same parameters.

        Args:
            existing_with_clause: The first branch's compiled CTE.
            existing_body: The ``with_cte()`` body it was compiled from.
            with_clause: This branch's compiled CTE.
            body: The ``with_cte()`` body it was compiled from.

        Returns:
            True when both define the same CTE.
        """
        if existing_body is body or existing_with_clause.query is with_clause.query:
            return True
        if isinstance(existing_with_clause.query, QueryBuilder) and isinstance(with_clause.query, QueryBuilder):
            return existing_with_clause.query.get_parameterized_sql() == with_clause.query.get_parameterized_sql()
        return False

    def _get_derived_table_query(self, combined_query: QueryBuilder | SetOperationQuery) -> QueryBuilder:
        """Selects every column of the set operations built so far from a derived table of them,
        so a following operator applies to their whole result.

        Args:
            combined_query: The set operations built so far.

        Returns:
            The query selecting from the derived table, in the same column order.
        """
        first_branch_query = (
            combined_query.base_query if isinstance(combined_query, SetOperationQuery) else combined_query
        )
        column_names = [select.alias or select.name for select in first_branch_query._selects]
        derived_table_query = self._db.query_class.from_(combined_query).select(
            *(combined_query.field(column_name) for column_name in column_names)
        )
        derived_table_query.wrap_set_operation_queries = False
        return derived_table_query

    # --- the build ------------------------------------------------------------------------------

    def _prepare_build(self) -> None:
        self._rows = self._get_rows()

    def _keeps_plan_built_into_another(self) -> bool:
        # Every branch records its own values - one keeping no plan records an empty reference.
        return True

    def _get_union_plan_description(self, connection_bound: bool) -> PlanDescription | None:
        """Describes this set operation - its structure is the plan key: each branch's structure
        as it is built in (with the connection it is pinned to), the operations, the ordering,
        whether it is sliced, and the zone the statement renders.

        Args:
            connection_bound: False for this query built into another one.

        Returns:
            The description, its values every branch's, then the LIMIT and the OFFSET - or None
            when a branch keeps no plan.
        """
        branch_structures: list[tuple[Any, ...]] = []
        values: list[Any] = []
        for branch in self._branches:
            branch_description = self._get_prepared_branch(branch).get_plan_description(PlanContext.EMPTY)
            if branch_description is None:
                return None
            branch_structures.append((branch.get_pinned_connection_name(), branch_description.structure))
            values.extend(branch_description.values)
        if self._limit is not None:
            values.append(self._limit)
        if self._offset:
            values.append(self._offset)
        plan_key = (
            CombinedQuery,
            self._combines_values,
            self.model,
            *self._get_connection_structure(connection_bound),
            self._get_visibility_structure(),
            tuple(branch_structures),
            self._set_operations,
            tuple(self._orderings),
            self._limit is not None,
            bool(self._offset),
            self._is_none,
            Timezone.get_rendered_zone_name(),
        )
        return PlanDescription(plan_key, values)

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        return self._get_scoped_copy()._get_union_plan_description(connection_bound=False)

    def _get_plan(self) -> tuple[PlanDescription | None, StatementPlan | None]:
        # The branches' descriptions decide whether the statement keeps a plan.
        description = self._get_union_plan_description(connection_bound=True)
        if description is None:
            return None, None
        return description, StatementPlans.find(description.structure)

    def _restore_from_plan(self, plan: StatementPlan) -> None:
        self._rows.restore(plan.result_reading)

    def _get_plan_record(self) -> dict[str, Any]:
        return {"result_reading": self._rows.get_result_reading()}

    def _build_statement(self, value_wrapper_refs: RecordedValueRefs | None, *, records_for_caller: bool) -> bool:
        combined_query: QueryBuilder | SetOperationQuery | None = None
        # Every branch's with_cte() CTEs are hoisted to the combined statement as a whole - a
        # WITH clause rendered on a branch would land after UNION/INTERSECT/EXCEPT.
        with_clauses: list[Any] = []
        # name -> (the first branch's CTE, the with_cte() body it was built from)
        with_clauses_by_name: dict[str, tuple[Any, Any]] = {}
        for branch_index, branch in enumerate(self._branches):
            branch_query, cte_bodies_by_name = self._get_branch_statement(branch, branch_index, value_wrapper_refs)
            for with_clause in branch_query._with:
                cte_body = cte_bodies_by_name.get(with_clause.alias, with_clause.query)
                existing = with_clauses_by_name.get(with_clause.alias)
                if existing is None:
                    with_clauses_by_name[with_clause.alias] = (with_clause, cte_body)
                    with_clauses.append(with_clause)
                elif not self._cte_bodies_match(*existing, with_clause, cte_body):
                    raise QueryError(
                        f"with_cte({with_clause.alias!r}, ...) is defined differently across "
                        "union()/intersection()/difference() branches - give each branch's own "
                        "CTE body a distinct name, or reuse the exact same .with_cte() call on "
                        "every branch."
                    )
            branch_query = copy(branch_query)
            branch_query._with = []
            branch_query.wrap_set_operation_queries = False
            if combined_query is None:
                combined_query = branch_query
                continue
            set_operation = self._set_operations[branch_index - 1]
            if set_operation == SetOperation.INTERSECT and any(
                previous_operation != SetOperation.INTERSECT
                for previous_operation in self._set_operations[: branch_index - 1]
            ):
                # INTERSECT binds tighter than UNION/EXCEPT on Postgres - the chain so far is
                # combined as one derived table, so it applies to all of it, as chained.
                combined_query = self._get_derived_table_query(combined_query)
            match set_operation:
                case SetOperation.UNION:
                    combined_query = combined_query.union(branch_query)
                case SetOperation.UNION_ALL:
                    combined_query = combined_query.union_all(branch_query)
                case SetOperation.INTERSECT:
                    combined_query = combined_query.intersect(branch_query)
                case SetOperation.EXCEPT_OF:
                    combined_query = combined_query.except_of(branch_query)
                case _:  # pragma: nocoverage
                    raise QueryError(f"Unsupported set operation: {set_operation!r}")
        derived_table = cast("QueryBuilder | SetOperationQuery", combined_query).as_(VALUES_SET_OPERATION_ALIAS)
        rows = self._rows
        query = self._db.query_class.from_(derived_table).select(
            *(derived_table.field(alias).as_(alias) for alias in rows.get_output_aliases())
        )
        for field_name, order in self._orderings:
            query = query.orderby(derived_table.field(rows.get_ordering_alias(field_name)), order=order)
        if self._limit is not None:
            query._limit = query._wrapper_cls(self._limit)
        if self._offset:
            query._offset = query._wrapper_cls(self._offset)
        query._with = with_clauses
        self.query = query
        if value_wrapper_refs is not None:
            # The slice is among the values - bound per query.
            for slice_bound_term in (query._limit, query._offset):
                if slice_bound_term is not None:
                    value_wrapper_refs.append((ValueRefOrigin.SUBQUERY, LiteralValueRef(slice_bound_term)))
        return True

    # --- running --------------------------------------------------------------------------------

    async def _execute(self) -> Any:
        sql, params = self._get_parameterized_sql()
        rows = await self._rows.read(self, sql, params)
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

    def _stream_batches(self, db: TransactionClient, chunk_size: int) -> AsyncGenerator[list[Any]]:
        return cast(
            "AsyncGenerator[list[Any]]",
            self._rows.stream_batches(self, db, *self._get_parameterized_sql(), chunk_size),
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

    def _get_single_column_value_field(self) -> Field[Any] | None:
        """The value field of the one combined column of values, once built.

        Returns:
            The field, or None for several columns, an unknown field or model instances.
        """
        rows = self._rows
        if not isinstance(rows, CombinedValuesRows) or len(rows.column_value_fields) != 1:
            return None
        return rows.column_value_fields[0]

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
        self, field_name: str, value_wrapper_refs: RecordedValueRefs | None = None
    ) -> QueryBuilder:
        """One column of combined model instances, as a query to embed - a queryset combining
        querysets passed as an ``__in`` filter value (``filter(pk__in=a.union(b))``).

        Args:
            field_name: The field to select, ``"pk"`` included.
            value_wrapper_refs: The list of the enclosing query the value references go into.

        Returns:
            The query selecting that column from the combined rows.

        Raises:
            QueryError: The model has a composite primary key.
            FieldError: The combined rows don't select the field.
        """
        pk_attr = self.model._meta.pk_attr
        if field_name == "pk":
            self.model._meta.raise_if_no_primary_key("a union used as an __in filter value")
            if isinstance(pk_attr, tuple):
                raise QueryError(
                    f"{self.model.__name__} has a composite primary key - a union can't be used as a "
                    "single-column __in filter value."
                )
            field_name = pk_attr
        return self._get_fields_values_query((field_name,), value_wrapper_refs)

    def _get_fields_values_query(
        self, field_names: Sequence[str], value_wrapper_refs: RecordedValueRefs | None = None
    ) -> QueryBuilder:
        """Columns of combined model instances, as a query to embed - a queryset combining
        querysets passed as an ``__in`` filter value, one column per compared key column.

        Args:
            field_names: The fields to select, in order.
            value_wrapper_refs: The list of the enclosing query the value references go into.

        Returns:
            The query selecting those columns from the combined rows.

        Raises:
            FieldError: The combined rows don't select one of the fields.
        """
        union = self._get_execution_query()
        if value_wrapper_refs is not None:
            union._make_subquery(value_wrapper_refs=value_wrapper_refs)
        else:
            union._make_subquery()
        rows_query = copy(union.query)
        columns = []
        for field_name in field_names:
            column_name = self.model._meta.fields_db_projection.get(field_name)
            if column_name not in union._rows.get_output_names():
                raise FieldError(f"The union doesn't select {field_name!r} - it can't be used as an __in filter value")
            columns.append(rows_query.field(column_name))
        return union._db.query_class.from_(rows_query).select(*columns)

    # --- the queryset's methods -----------------------------------------------------------------

    def _get_ordered_queryset(self, orderings: tuple[str | Ordering, ...]) -> QuerySet[Any, Any]:
        """The combined rows ordered by their columns (``"name"``, ``"-name"``, or an
        ``Ordering`` for explicit NULL placement) - values by their output names, model instances
        by their fields and annotations, ``pk`` being the primary key.

        Args:
            orderings: The ordering names or ``Ordering`` expressions.

        Returns:
            The queryset.

        Raises:
            QueryError: A name isn't an output name of the values.
            QueryError: The rows are sliced - order them before slicing.
        """
        if self._limit is not None or self._offset:
            raise QueryError("Cannot reorder a query once a slice has been taken.")
        parsed_orderings: list[tuple[str, Order]] = []
        for ordering in orderings:
            field_name, order = self._get_ordering_string(ordering)
            if field_name == "pk" and not self._combines_values:
                parsed_orderings.extend((pk_attr_name, order) for pk_attr_name in self.model._meta.pk_attr_names)
                continue
            self._rows.check_ordering_name(field_name)
            parsed_orderings.append((field_name, order))
        queryset = self._source_queryset._clone()
        queryset._orderings = parsed_orderings
        return queryset

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
        exception: type[BaseException] | BaseException | None,
        raise_does_not_exist: bool,
    ) -> QuerySet[Any, Any]:
        """The one combined row. Conditions filter every branch before they are combined - a row
        condition gives the same rows before or after UNION/INTERSECT/EXCEPT.

        Args:
            args: ``Q`` conditions.
            kwargs: Filter keyword arguments.
            exception: Raised instead of ``DoesNotExist`` when there is no row.
            raise_does_not_exist: Raise when there is no row.

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
                    "get()/get_or_none() with conditions can't be used on a sliced union - the conditions "
                    "filter the branches, before the slice. Pass them before slicing."
                )
            queryset = self._get_changed_branches_queryset(queryset, lambda branch: branch.filter(*args, **kwargs))
        queryset._single = True
        queryset._raise_does_not_exist = raise_does_not_exist
        queryset._does_not_exist_exception = exception
        queryset._limit = (
            GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK
            if self._limit is None
            else min(self._limit, GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK)
        )
        return queryset

    @staticmethod
    def _get_changed_branches_queryset(
        queryset: QuerySet[Any, Any], change_branch: Callable[[QuerySet[Any, Any]], QuerySet[Any, Any]]
    ) -> QuerySet[Any, Any]:
        """A copy of a queryset combining querysets with every queryset it combines changed.

        Args:
            queryset: The queryset.
            change_branch: Changes a queryset that combines none.

        Returns:
            The copy.
        """
        combination = cast("Combination", queryset._combination)
        changed_queryset = queryset._clone()
        changed_queryset._combination = replace(
            combination,
            branches=tuple(
                CombinedQuery._get_changed_branches_queryset(branch, change_branch)
                if branch._combination is not None
                else change_branch(branch)
                for branch in combination.branches
            ),
        )
        return changed_queryset

    def _get_prefetching_queryset(self, relations: tuple[str | Prefetch, ...]) -> QuerySet[Any, Any]:
        """The combined model instances with relations prefetched on them once they are read -
        batched over the instances, grouped by their model.

        Args:
            relations: Relation names or ``Prefetch(...)`` instances.

        Returns:
            The queryset.

        Raises:
            QueryError: The rows are values - there is nothing to attach a relation to.
        """
        if self._combines_values:
            raise QueryError(
                ".values()/.values_list() cannot be used with prefetch_related() - the result is plain "
                "tuples/dicts, not model instances, so there's nothing to attach a prefetched relation to."
            )
        queryset = self._source_queryset._clone()
        queryset._combination = replace(
            cast("Combination", queryset._combination),
            prefetched_relations=self._prefetched_relations + tuple(relations),
        )
        return queryset

    def _get_values_queryset(
        self, select_values: Callable[[QuerySet[Any, Any]], QuerySet[Any, Any]]
    ) -> QuerySet[Any, Any]:
        """The same set operation over ``.values()``/``.values_list()`` of every branch, like
        Django. The ordering and slice of the combined rows carry over.

        Args:
            select_values: Applies the ``.values()``/``.values_list()`` call to a branch.

        Returns:
            The queryset combining the values.

        Raises:
            QueryError: The rows are values already, the combined model instances prefetch
                relations, or are ordered by a field the values don't select.
        """
        if self._combines_values:
            raise QueryError(
                ".values()/.values_list() can't be used on a set operation of .values()/.values_list() "
                "querysets - call it on each queryset before combining them."
            )
        if self._prefetched_relations:
            raise QueryError(
                ".values()/.values_list() cannot be used with prefetch_related() - the result is plain "
                "tuples/dicts, not model instances, so there's nothing to attach a prefetched relation to."
            )
        queryset = self._get_changed_branches_queryset(self._source_queryset, select_values)
        queryset._combination = replace(cast("Combination", queryset._combination), prefetched_relations=())
        first_leaf = cast("ValuesQuery", CombinedQuery(queryset)._get_first_leaf())
        output_name_by_selected_name = dict(
            zip(first_leaf._get_output_field_names(), first_leaf._get_output_names_for_set_operation(), strict=True)
        )
        orderings: list[tuple[str, Order]] = []
        for field_name, order in self._orderings:
            output_name = output_name_by_selected_name.get(field_name)
            if output_name is None:
                raise QueryError(
                    f"The union is ordered by {field_name!r}, which .values()/.values_list() doesn't select - "
                    "select it too."
                )
            orderings.append((output_name, order))
        queryset._orderings = orderings
        return queryset
