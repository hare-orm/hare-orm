from __future__ import annotations

from collections.abc import (
    Coroutine,
    Sequence,
)
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar, cast

from hare.core.routing.written_connections import WrittenConnections
from hare.dialects.base.client.database_client import DatabaseClient, retryable_read_query_active
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.exceptions import (
    ConfigurationError,
    QueryError,
)
from hare.fields.field import Field as ModelField
from hare.query.expressions import (
    Q,
)
from hare.query.expressions.constants import UNBINDABLE_VALUE_ORIGIN
from hare.query.expressions.subqueries.outer_query_state import outer_expression_context
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.recording.plan_recording import PlanRecording
from hare.query.plans.statement.query_key_compiler import QueryKeyCompiler
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plan_runs import StatementPlanRuns
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions
from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup
from hare.query.queryset.pending_calls.pending_filter_calls import PendingFilterCalls
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.queryset.single_rows.get_exceptions import GetExceptions
from hare.query.scopes.row_scopes import RowScopes
from hare.query.statements.building.query_joins import QueryJoins
from hare.sql import Table
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.enums import Equality
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class AwaitableQuery(QuerySpecification[TModel], Generic[TModel], abstract=True):
    #: Whether this type of query only reads - a read is retried on a lost connection, a write
    #: never: retried blindly it could take effect twice.
    is_read_only: ClassVar[bool] = False

    #: Whether an implicit GROUP BY always includes the primary key - one group per row. True for
    #: every query standing for model rows; False for .values()/.values_list(), which group by the
    #: fields they name.
    group_by_must_include_primary_key: ClassVar[bool] = True

    #: Whether QueryOrdering.get_ordering() may reference a selected annotation by its SELECT alias. False for
    #: UPDATE/DELETE, whose ordered pk subquery selects only the primary key.
    ordering_can_reference_annotation_alias: ClassVar[bool] = True

    #: Whether an annotated aggregate's value is never read - only whether the query returned a row
    #: (exists()). Two aggregated to-many relations in one query, which inflate each other's counts,
    #: are fine then.
    aggregate_value_is_unused: ClassVar[bool] = False

    #: Whether a filter reading a window function is applied to the query wrapped as a derived
    #: table instead of rejected - True for .values()/.values_list() (ValuesQuery).
    window_filter_wrapping_supported: ClassVar[bool] = False

    __slots__ = (
        "query",
        "_joined_tables",
        "_joined_tables_set",
        # One resolved term per annotation name and table for the current build, so a GROUP BY
        # and an ORDER BY of the same unselected annotation share one term - see
        # QueryAnnotations.get_annotation_expression_term().
        "_annotation_expression_terms",
        # The value references recorded while each annotation name was resolved again for one of
        # those clauses - see QueryAnnotations.get_expression_term_value_references().
        "_expression_term_references",
        # The ORDER BY term of each annotation the query orders by, for a DISTINCT ON that must
        # repeat it - the SELECT alias of a selected one, whose expression's parameters would
        # differ from the alias's.
        "_annotation_ordering_terms",
        # Populated fresh by QueryAnnotations.get_annotate() on every _make_query() call, read back by
        # ValuesOutput.get_selected_value_field() - see both for why this lives here instead
        # of on the (possibly shared/reused-across-queries) annotation expression object itself.
        "_annotation_output_fields",
        # Set by QueryConditions.get_filters() (via QueryAnnotations.get_annotate()), read back by
        # QueryGrouping.apply_auto_group_by() once the caller's own select list is fully finalized - see
        # that method's own docstring
        # for why the two can't just be one step.
        "_has_aggregate",
        # Set by QueryConditions.get_filters(): the filters (and keyset cursor) reading a window function, which
        # ValuesQuery applies to the built query wrapped as a derived table.
        "_window_filter_criterion",
        # Set on the copy a query makes of itself to run once - see _get_execution_query().
        "_is_execution_query",
        # The SQL text and parameters a plan hit of a running query left - see StatementPlanRuns.run_on_plan().
        "_compiled_statement",
        # The plan the last build found for this query, or recorded - None for a query that keeps
        # no plan.
        "_statement_plan",
        # The arguments of StatementPlanRuns.record_plan() for a query whose dialect QuerySet method calls
        # are applied after the build - recorded once they are (see _make_query()).
        "_deferred_plan_record",
        # The list the build records its value references into, None when it records none - the
        # conditions of the dialect's QuerySet method calls, applied after the build, record into it
        # too.
        "_extension_value_wrapper_references",
        # Set by _make_query_to_run(): the query is built to be run by itself, not shown or
        # embedded in another one.
        "_runs_compiled_statement",
        # The calls the queryset this query runs was made with, when it was made by simple calls
        # alone (QuerySet._call_signature) - None otherwise - and the values of their filters.
        "_call_signature",
        "_call_values",
        # The key of the plan of the calls, the keys of its values and how many values the query
        # class binds after them, set when no plan was kept under the key yet - the plan this run
        # builds is kept under it too.
        "_call_signature_record",
        # The object this query, made again for each description or build of a query it is built
        # into, stands for - unset otherwise (PlanOrigins).
        "_plan_origin",
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # The description of the statement of each declaration of the class's plan slots.
        QueryKeyCompiler.compile_declared(cls)

    #: The statement being built.
    query: QueryBuilder

    #: What ``query`` holds before a build - one builder no query ever changes.
    EMPTY_QUERY: ClassVar[QueryBuilder] = QueryBuilder()

    def _apply_connection(self, connection: DatabaseClient | None) -> None:
        """Binds this query to the connection it runs on - the statement being built starts
        over in that connection's builder.

        Args:
            connection: The database connection to use for this query.
        """
        super()._apply_connection(connection)
        if connection is not None and hasattr(self, "query"):
            self.query = connection.query_class.get_empty_builder()

    def __init__(self, model: type[TModel]) -> None:
        super().__init__(model)
        self._init_build_state()

    def _init_build_state(self) -> None:
        """Sets what a build of the query fills in."""
        self._joined_tables: list[Table] = []
        self._joined_tables_set: set[Table] = set()
        self._annotation_expression_terms: dict[tuple[str, Table], Term] = {}
        self._expression_term_references: dict[str, RecordedValueReferences] = {}
        self._annotation_ordering_terms: dict[str, Term] = {}
        self.query = self.EMPTY_QUERY
        self._annotation_output_fields: dict[str, ModelField[Any] | None] = {}
        self._has_aggregate: bool = False
        self._window_filter_criterion: Criterion | None = None
        self._is_execution_query: bool = False
        self._compiled_statement: tuple[str, list[Any]] | None = None
        self._statement_plan: StatementPlan | None = None
        self._deferred_plan_record: dict[str, Any] | None = None
        self._extension_value_wrapper_references: RecordedValueReferences | None = None
        self._runs_compiled_statement: bool = False
        self._call_signature: tuple[Any, ...] | None = None
        self._call_values: tuple[Any, ...] = ()
        self._call_signature_record: tuple[tuple[Any, ...], Sequence[str], int] | None = None

    def _get_single_or_list_result(self, rows: list[Any]) -> Any:
        """The fetched rows as returned - reversed back for a ``.before_cursor()`` query, or the
        one row of a single-row query.

        Args:
            rows: The fetched rows.

        Returns:
            The rows, or the one row (None when there is none).

        Raises:
            DoesNotExist: A ``.get()`` query matched no row.
            MultipleObjectsReturned: A single-row query matched more than one row.
        """
        if self._single:
            if len(rows) == 1:
                return rows[0]
            if not rows:
                if self._raise_does_not_exist:
                    GetExceptions.raise_object_does_not_exist(self)
                return None
            GetExceptions.raise_multiple_objects_returned(self)
        if self._reverse_result_order:
            return rows[::-1]
        return rows

    def _get_row_join_lookups(self) -> list[str]:
        """Lookups this query joins outside ``QueryConditions.get_filters()`` - its ordering and selected fields -
        whose to-many hops count towards the aggregate fan-out check.

        Returns:
            The lookups.
        """
        return []

    def _get_group_key_lookups(self) -> list[str]:
        """Field lookups of this query's GROUP BY keys - the explicit ``.group_by()`` fields.

        Returns:
            The lookups.
        """
        return QueryJoins.get_field_lookups(self, self._group_bys)

    def _aggregates_per_model_row(self) -> bool:
        """Whether a query selecting chosen columns computes its aggregates per model row - they
        were annotated before ``.values()``/``.values_list()``, like Django.

        Returns:
            False - only a ``.values()``/``.values_list()`` query decides otherwise.
        """
        return False

    def _annotation_renders_as_expression(self, annotation_name: str) -> bool:
        """Whether a GROUP BY/ORDER BY of an annotation renders its full expression rather than
        its SELECT alias.

        Args:
            annotation_name: The annotation name.

        Returns:
            True when the annotation isn't selected, or ORDER BY can't reference an alias.
        """
        return annotation_name in self._alias_keys or not self.ordering_can_reference_annotation_alias

    def _get_filter_value_query(self) -> AwaitableQuery[Any]:
        """The query a filter compares with when this query is its value.

        Returns:
            This query itself; a bare queryset selects its primary key instead.
        """
        return self

    def _get_scoped_copy(self) -> Self:
        """A copy of this query with its default scope applied - what a query built into another one (a
        subquery, a CTE body, a set operation's branch) describes in its ``get_plan_description()``.

        Returns:
            The copy.
        """
        if self._pending_filter_calls:
            # Built once, so every copy holds the same conditions (_build_conditions_for_copies()).
            PendingFilterCalls.build_pending_filter_calls(self)
        scoped_query = copy(self)
        scoped_query._plan_origin = self
        scoped_query._apply_ambient_scope()
        return scoped_query

    def _get_statements(self, parameters_inline: bool) -> list[tuple[str, list[Any]]]:
        self._make_query()
        if parameters_inline:
            return [(self.query.get_sql(), [])]
        sql, values = self.query.get_parameterized_sql()
        return [(sql, values)]

    def _make_query(self, **kwargs: Any) -> None:
        """Builds ``self.query`` for the context the query runs in: its default scope first.

        Args:
            kwargs: Passed on to ``_build_query()``.
        """
        CallsBeforeSetup.raise_if_invalid(self)
        self._compiled_statement = None
        self._statement_plan = None
        self._deferred_plan_record = None
        self._extension_value_wrapper_references = None
        self._apply_ambient_scope()
        self._build_query(**kwargs)
        if self._extension_calls:
            self._apply_extension_calls()

    def _apply_extension_calls(self) -> None:
        """Applies the calls of the dialect's QuerySet methods to the query just built, then
        records its plan - the plan's SQL text holds the calls. A query run on its plan has them
        in the text already.

        Raises:
            UnSupportedError: The connection's dialect has no implementation of a called method.
        """
        if self._compiled_statement is not None:
            QuerySetExtensions.check(self)
            return
        QuerySetExtensions.apply(self, self._extension_value_wrapper_references)
        deferred_plan_record = self._deferred_plan_record
        if deferred_plan_record is not None:
            self._deferred_plan_record = None
            # A copy of the plan's query with other values in its terms would miss the calls.
            StatementPlanRuns.record_plan(self, **deferred_plan_record, extension_calls_applied=True)

    def _make_query_to_run(self) -> None:
        """Builds the query to be run by itself - the only build a plan's compiled statement may stand
        in for. A query shown with ``.sql()``/``explain()`` or built into another one is built with
        ``_make_query()``. A query of a queryset made by simple calls alone runs on the plan kept
        under the key of the calls, built not even in part.
        """
        self._runs_compiled_statement = True
        if self._call_signature is not None and StatementPlanRuns.run_on_call_signature_plan(self):
            return
        self._make_query()

    def _get_call_signature_type_part(self) -> tuple[Any, list[Any]]:
        """What the key of the calls and the values bound hold of this type of query beyond its
        calls - the values a write assigns, say.

        Returns:
            The structure (None when the query isn't run by the key of its calls) and the values,
            bound after the calls' own.
        """
        return (), []

    def _prepare_call_signature_run(self) -> None:
        """Works out what running on a plan of the calls needs - what a build would before looking
        its plan up (``_prepare_build()``)."""
        self._prepare_build()

    def _get_parameterized_sql(self) -> tuple[str, list[Any]]:
        """The statement to run: the compiled one running on a plan left, otherwise
        ``self.query`` rendered.

        Returns:
            The SQL text and its parameters.
        """
        compiled_statement = self._compiled_statement
        if compiled_statement is not None:
            return compiled_statement
        return self.query.get_parameterized_sql()

    #: Whether a plan's statement takes the LIMIT and OFFSET of the query running on it.
    plan_binds_slice: ClassVar[bool] = False

    def _build_query(self, *, value_wrapper_references: RecordedValueReferences | None = None) -> None:
        """Builds ``self.query`` - the one way a query of any type is built: prepared, then run on
        the plan kept for it (``StatementPlans``), or built in full and kept as the plan.

        Args:
            value_wrapper_references: The list of an enclosing query this query, built into it (a
                subquery, a CTE body, a branch of a set operation), records its value references
                into - in full, with no plan of its own looked up or kept. A query that keeps no
                plan records an empty reference there, so the enclosing query keeps none either.
        """
        records_for_caller = value_wrapper_references is not None
        # The conditions the build folds into JOINs are recorded from the start - preparing builds
        # JOINs too (.only() across a relation). Built into another query, they record into that
        # query's recording.
        plan_recording = PlanRecording()
        recording_token = None if records_for_caller else PlanRecording.current.set(plan_recording)
        try:
            QueryJoins.reset_joined_tables(self)
            self._annotation_output_fields = {}
            self._prepare_build()
            if records_for_caller:
                if not self._keeps_plan_built_into_another():
                    cast("RecordedValueReferences", value_wrapper_references).append((UNBINDABLE_VALUE_ORIGIN, None))
                self._extension_value_wrapper_references = value_wrapper_references
                self._build_statement(value_wrapper_references, records_for_caller=True)
                return
            # A setting written into the SQL text as it is (a sample's percent and seed) keeps no plan.
            description, plan = (None, None) if self._options.keeps_no_plan() else self._get_plan()
            if (
                description is not None
                and plan is not None
                and StatementPlanRuns.find_plan(
                    self,
                    description.structure,
                    self._get_plan_values(description, plan),
                    paginate=self.plan_binds_slice,
                    plan=plan,
                )
            ):
                self._restore_from_plan(plan)
                return
            value_wrapper_references = [] if description is not None else None
            self._extension_value_wrapper_references = value_wrapper_references
            # Described again with the origins of its values before the build changes the query - as
            # a later query of the key describes itself.
            origins_description = self._get_origins_description() if description is not None else None
            keeps_plan = self._build_statement(value_wrapper_references, records_for_caller=False)
        finally:
            if recording_token is not None:
                PlanRecording.current.reset(recording_token)
        if description is not None and keeps_plan:
            StatementPlanRuns.record_plan(
                self,
                self._get_plan_key(description),
                value_wrapper_references,
                self._get_plan_record_description(origins_description),
                binds_ctes=True,
                plan_recording=plan_recording,
                **self._get_plan_record(),
            )

    def _prepare_build(self) -> None:
        """Works out what both a run on a plan and a full build need - called before the plan is
        looked up, so a check made here is made on a plan hit too."""

    def _keeps_plan_built_into_another(self) -> bool:
        """Whether this query, built into another one, lets that query keep a plan - it records
        every value it holds.

        Returns:
            True when it does.
        """
        # The enclosing query's key holds this query's structure; whether a query correlated to it
        # aliases its own table is decided by the two tables alone, in that key as well.
        outer_context_token = outer_expression_context.set(None)
        try:
            return StatementPlanDescriptions.query_is_plannable(self)
        finally:
            outer_expression_context.reset(outer_context_token)

    def _get_build_plan_description(self) -> PlanDescription | None:
        """Describes the statement this query builds - the structure is the plan key, the values
        are bound into the plan's statement.

        Returns:
            The description, None for a query that keeps no plan.
        """
        return None

    def _describe_statement(self, built_into_another: bool) -> PlanDescription | None:
        """Describes this query's statement from its class's ``plan_slots`` - generated for a class
        declaring them (``QueryKeyCompiler``).

        Args:
            built_into_another: Whether the query is built into another one - the dialect and the
                connection are that query's, and its slice is among its values.

        Returns:
            The description, None for a query that keeps no plan.
        """
        raise NotImplementedError(f"{type(self).__name__} declares no plan_slots")

    def _get_plan(self) -> tuple[PlanDescription | None, StatementPlan | None]:
        """The description of this query's statement and the plan kept under its key.

        Returns:
            ``(None, None)`` for a query that keeps no plan; the description alone for the first
            query of its key.
        """
        description = self._get_build_plan_description()
        if description is None:
            return None, None
        # A plan kept under the key is the answer to whether the query keeps one - except inside a
        # correlated subquery, where that also depends on the enclosing query.
        plan = StatementPlans.find(description.structure) if outer_expression_context.get() is None else None
        if plan is None and not StatementPlanDescriptions.query_state_is_plannable(self):
            return None, None
        return description, plan

    def _get_plan_key(self, description: PlanDescription) -> tuple[Any, ...]:
        """The key the plan of ``description`` is kept under."""
        return description.structure

    def _get_plan_values(self, description: PlanDescription, plan: StatementPlan) -> list[Any]:
        """This query's values ``plan`` binds, in the order its description lists them."""
        return description.values

    def _get_origins_description(self) -> PlanDescription | None:
        """This query's description with the origin of each value, made before the build."""
        return PlanOrigins.describe(lambda: self._get_plan()[0])

    def _get_plan_value_description(self, plan: StatementPlan) -> PlanDescription | None:
        """The values of this query the plan it runs on binds, each with its origin - in the order
        ``_get_plan_values()`` lists them.

        Args:
            plan: The plan.

        Returns:
            The description.
        """
        return self._get_origins_description()

    def _get_plan_record_description(self, origins_description: PlanDescription | None) -> PlanDescription | None:
        """The values of the query just built a plan binds, each with its origin - in the order
        ``_get_plan_values()`` lists them.

        Args:
            origins_description: The description made before the build (``_get_origins_description()``).

        Returns:
            The description.
        """
        return origins_description

    def _restore_from_plan(self, plan: StatementPlan) -> None:
        """Takes from the plan this query runs on what reading the result needs - a full build
        works it out while building."""

    def _get_plan_record(self) -> dict[str, Any]:
        """What else the plan of the query just built keeps (``StatementPlanRuns.record_plan()``'s arguments)."""
        return {}

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        """Builds ``self.query`` in full.

        Args:
            value_wrapper_references: The list the value references are recorded into, None when no plan
                is recorded.
            records_for_caller: Whether the list is an enclosing query's - the query is built into
                it.

        Returns:
            False when the statement built can't be kept as a plan after all.
        """
        raise NotImplementedError()  # pragma: nocoverage

    def _apply_ambient_scope(self) -> None:
        """Resolves the tenant the query runs for and, for a query under its model's default scope,
        puts that scope's filters in front of its own - replacing the ones a previous compilation
        put there.

        Raises:
            QueryError: ``Hare.init()`` hasn't run, or the model is tenant-scoped and no
                tenant is active.
        """
        if not self.model._meta.basetable.get_table_name():
            raise ConfigurationError(
                f"Can't run a query on {self.model.__name__} - Hare.init() hasn't set up its connections yet"
            )
        # Resolved once per build - every JOIN and every query made from this one is scoped to
        # the same tenant as its own filters.
        visibility = self._visibility = self._visibility.get_for_active_tenant()
        ambient_filters = RowScopes.of(self.model).get_filters(visibility, uses_default_scope=self._uses_default_scope)
        ambient_q_objects = [Q(**{field_name: value}) for field_name, value in ambient_filters]
        if ambient_q_objects or self._ambient_q_count:
            for ambient_q in ambient_q_objects:
                # Made again by each copy of the query - its values come from the query.
                ambient_q._plan_origin = self
            self._q_objects = [*ambient_q_objects, *self._q_objects[self._ambient_q_count :]]
            self._ambient_q_count = len(ambient_q_objects)

    def _get_execution_query(self, for_write: bool = False) -> Self:
        """A copy of this query bound to the connection it runs on for one execution - a queryset
        run again later, or by several tasks at once, compiles into its own copy each time.

        Args:
            for_write: Whether the connection is chosen for a write.

        Returns:
            The copy, or this query itself when it already is one.
        """
        execution_query = self if self._is_execution_query else self.__copy__()
        execution_query._is_execution_query = True  # type: ignore[attr-defined]
        if execution_query._connection is None:
            execution_query._apply_connection(execution_query.get_connection(for_write=for_write))  # type: ignore[unreachable]
        if for_write and WrittenConnections.is_recording:
            WrittenConnections.record(execution_query._connection.connection_alias)
        return execution_query  # type: ignore[return-value]

    def _make_subquery(self, **kwargs: Any) -> None:
        """Builds ``self.query`` to be embedded in an enclosing query; a ``.none()`` queryset
        gets an always-false condition there, since it never reaches the ``_is_none`` check done
        at execution time.

        Args:
            kwargs: Passed on to ``_build_query()`` - ``value_wrapper_references``, the enclosing query's
                list its value references are recorded into, for a type that describes its plan
                (``plannable``).
        """
        self._make_query(**kwargs)
        self._apply_none_as_subquery()

    def _apply_none_as_subquery(self) -> None:
        if self._is_none:
            self.query = self.query.where(
                BasicCriterion(
                    Equality.EQ,
                    ValueWrapper(1, allow_parametrize=False),
                    ValueWrapper(0, allow_parametrize=False),
                )
            )

    async def _execute(self) -> Any:
        raise NotImplementedError()  # pragma: nocoverage

    def _raise_if_row_lock_outside_transaction(self) -> None:
        """Rejects a ``select_for_update()`` query run outside a transaction.

        Raises:
            QueryError: The query runs outside a transaction.
        """
        if not isinstance(self._connection, TransactionClient):
            raise QueryError(
                "select_for_update() requires an active Transactions.atomic() block - a "
                "SELECT ... FOR UPDATE lock is only meaningful for the lifetime of the enclosing "
                "transaction; used outside one, the lock is acquired and immediately released "
                "(autocommit), giving no real protection while looking like it does"
            )

    async def _execute_with_retry_context(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Awaits the query's execution - for a read-only query with the connection-loss retry context
        active, so only reads are retried. Rejects a ``select_for_update()`` query run outside a
        transaction.
        """
        if self._select_for_update:
            try:
                self._raise_if_row_lock_outside_transaction()
            except QueryError:
                coro.close()
                raise
        if not type(self).is_read_only or not self._connection.read_retry_max_retries:
            return await coro
        token = retryable_read_query_active.set(True)
        try:
            return await coro
        finally:
            retryable_read_query_active.reset(token)
