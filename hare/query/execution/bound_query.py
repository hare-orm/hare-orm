from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar

from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.core.routing.written_connections import WrittenConnections
from hare.fields.field import Field as ModelField
from hare.query.expressions import Q
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.query.queryset.pending_calls.pending_filter_calls import PendingFilterCalls
from hare.query.queryset.query_specification import QuerySpecification
from hare.sql import Table
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset.extensions.declarations import BoundExtensionCall
    from hare.query.queryset.extensions.query_set_extension_result import QuerySetExtensionResult

TModel = TypeVar("TModel", bound="Model")


class BoundQuery(QuerySpecification[TModel], Generic[TModel], abstract=True):
    """A declaration being prepared for a connection and one execution.

    SQL builders, bound annotations, joins, parameters and ambient conditions live here.
    Copying a declaration into a statement transfers declared filters only; a later execution
    obtains its own connection and ambient scope. None of these results is stored on QuerySet.
    """

    EMPTY_QUERY: ClassVar[QueryBuilder] = QueryBuilder()
    query: QueryBuilder
    _connection: DatabaseClient

    mutable_clone_slots: ClassVar[dict[str, Callable[[], Any]]] = QuerySpecification.MUTABLE_SPECIFICATION_SLOTS | {
        "_joined_tables": list,
        "_joined_tables_set": set,
        "_annotation_expression_results": dict,
        "_expression_term_references": dict,
        "_annotation_ordering_terms": dict,
        "_annotation_output_fields": dict,
    }

    __slots__ = (
        "query",
        "_bound_extensions",
        "_extension_result",
        "_applies_result_transform",
        "_ambient_conditions",
        "_joined_tables",
        "_joined_tables_set",
        # Bound expression results for this build, shared by clauses using an annotation.
        # The field and joins belong to the result alongside its SQL term.
        "_annotation_expression_results",
        # The value references recorded while each annotation name was resolved again for one of
        # those clauses - see QueryAnnotations.get_expression_term_value_references().
        "_expression_term_references",
        # The ORDER BY term of each annotation the query orders by, for a distinct by fields that must
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
        # Set when the build applied the calls of the dialect's QuerySet methods itself - to the
        # query reading the rows, before wrapping it in a derived table.
        "_extension_calls_built_in",
        # The calls the queryset this query runs was made with, when it was made by simple calls
        # alone (QuerySet._call_signature) - None otherwise - and the values of their filters.
        "_call_signature",
        "_call_values",
        # The key of the plan of the calls, the keys of its values and how many values the query
        # class binds after them, set when no plan was kept under the key yet - the plan this run
        # builds is kept under it too.
        "_call_signature_record",
        # The object this query, made again for each description or build of a query it is built
        # into, stands for - None otherwise (PlanOrigins).
        "_plan_origin",
    )

    def _init_build_state(self) -> None:
        """Sets what a build of the query fills in."""
        self._ambient_conditions: tuple[Q, ...] = ()
        self._joined_tables: list[Table] = []
        self._joined_tables_set: set[Table] = set()
        self._annotation_expression_results: dict[tuple[str, Table], ExpressionResult] = {}
        self._expression_term_references: dict[str, RecordedValueReferences] = {}
        self._annotation_ordering_terms: dict[str, Term] = {}
        self.query = self.EMPTY_QUERY
        self._bound_extensions: tuple[BoundExtensionCall, ...] | None = None
        self._extension_result: QuerySetExtensionResult | None = None
        self._applies_result_transform = True
        self._annotation_output_fields: dict[str, ModelField[Any] | None] = {}
        self._has_aggregate: bool = False
        self._window_filter_criterion: Criterion | None = None
        self._is_execution_query: bool = False
        self._statement_plan: StatementPlan | None = None
        self._deferred_plan_record: dict[str, Any] | None = None
        self._extension_value_wrapper_references: RecordedValueReferences | None = None
        self._extension_calls_built_in: bool = False
        self._call_signature: tuple[Any, ...] | None = None
        self._call_values: tuple[Any, ...] = ()
        self._call_signature_record: tuple[tuple[Any, ...], Sequence[str], int] | None = None
        self._plan_origin: Any = None

    def _get_execution_query(self, for_write: bool = False, connection: DatabaseClient | None = None) -> Self:
        """A copy of this query bound to the connection it runs on for one execution - a queryset
        run again later, or by several tasks at once, compiles into its own copy each time.

        Args:
            for_write: Whether the connection is chosen for a write.
            connection: The connection this execution already chose - chosen here when None.

        Returns:
            The copy, or this query itself when it already is one.
        """
        execution_query = self if self._is_execution_query else self.__copy__()
        execution_query._is_execution_query = True
        bound_connection: DatabaseClient | None = execution_query._connection
        if bound_connection is None:
            bound_connection = (
                execution_query.get_connection(for_write=for_write) if connection is None else connection
            )
            execution_query._apply_connection(bound_connection)
        if execution_query.model._meta.checks_tenant_client or bound_connection.sets_tenants:
            ModelConnectionChoice.get_checked(execution_query.model, bound_connection)
        if for_write and WrittenConnections.is_recording:
            WrittenConnections.record(bound_connection.connection_alias)
        return execution_query

    @property
    def _q_objects(self) -> list[Q]:
        """Declared conditions with this build's ambient scope, without rewriting the declaration."""
        if self._pending_filter_calls:
            PendingFilterCalls.build_pending_filter_calls(self)
        ambient = getattr(self, "_ambient_conditions", ())
        return [*ambient, *self._q_object_list] if ambient else self._q_object_list

    @_q_objects.setter
    def _q_objects(self, conditions: list[Q]) -> None:
        self._pending_filter_calls = ()
        self._q_object_list = conditions
        self._ambient_conditions = ()
