from __future__ import annotations

from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar, cast

from hare.core.hare_context import HareContext
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.dialect import Dialect
from hare.dialects.base.features import Features
from hare.exceptions import (
    ConfigurationError,
    QueryError,
    UnSupportedError,
)
from hare.models.tenancy.tenancy import Tenancy
from hare.query.constants import AMBIENT_TENANT_NOT_OVERRIDDEN
from hare.query.enums import Connector, SpecificationSlotPlanRole
from hare.query.expressions import (
    Expression,
    ExpressionContext,
    Ordering,
    Q,
)
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.subqueries.outer_query_state import outer_expression_context
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.query.query_connection import QueryConnection
from hare.query.queryset.annotation_access_tracker import AnnotationAccessTracker
from hare.query.queryset.constants import FORBIDDEN_ANNOTATION_NAME_PATTERN
from hare.query.queryset.extensions.query_set_extension_call import QuerySetExtensionCall
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions
from hare.query.queryset.lazy_relation_names import LazyRelationNames
from hare.query.queryset.options.query_option import QueryOption
from hare.query.queryset.options.query_options import QueryOptions
from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup
from hare.query.queryset.pending_calls.pending_filter_calls import PendingFilterCalls
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.relation_loading.prefetching.prefetch import Prefetch
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql import Order, Table
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.identifiers import Identifiers
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset.combination.combination import Combination
    from hare.query.queryset.queryset import QuerySet
    from hare.query.queryset.selection.values_selection import ValuesSelection

TModel = TypeVar("TModel", bound="Model")

TDerivedAwaitableQuery = TypeVar("TDerivedAwaitableQuery", bound="QuerySpecification[Any]")


class QuerySpecification(Plannable, Generic[TModel], abstract=True):
    """What a query asks for - the model, the connection choice, the filters, annotations, ordering,
    slice and the other settings the chained methods declare. ``QuerySet`` adds the methods changing
    it; an ``AwaitableQuery`` takes one over, builds and runs its SQL.
    """

    __slots__ = (
        "model",
        "_connection",
        "_features",
        # Whether .using() pinned the connection - _db is also set once the router or the default
        # connection chose it. A pinned connection is offered to the prefetch queries.
        "_connection_explicitly_chosen",
        # The connection the parent queryset pinned with .using(), offered to its prefetch query:
        # taken after the router and before the model's default connection.
        "_router_fallback_connection",
        # Set by a relation read off a model instance: the alias of the connection the instance was
        # loaded from or saved to - taken after the router and before the model's default
        # connection, looked up when the query runs.
        "_instance_connection_alias",
        "_annotations",
        "_q_object_list",
        # Incremented once per .filter()/.exclude() call - the generation its Q objects are stamped
        # with.
        "_filter_call_counter",
        # The filter() calls of plain values a queryset made by simple calls alone keeps unbuilt -
        # (negate, filter-call generation, kwargs) each - built into _q_object_list on the first
        # read of _q_objects. A query running on the plan of its calls never builds them.
        "_pending_filter_calls",
        # Shared by QuerySet and the queries built from it.
        "_single",
        "_raise_does_not_exist",
        "_limit",
        "_offset",
        "_orderings",
        "_distinct",
        # Set by QuerySet.none(): every query built from the queryset gives its empty result without
        # touching the database.
        "_is_none",
        # Set by Manager.get_queryset() and handed to every query built from such a queryset: the
        # query runs under the model's default scope, resolved when it is compiled.
        "_uses_default_scope",
        # Which rows the default scopes let the query see - its own model's and those of
        # every model it JOINs.
        "_visibility",
        # The rarely set settings, shared with clones - see QueryOptions and the QueryOption
        # attributes below.
        "_options",
        # How many leading entries of _q_objects a build's default scope put there - a query
        # taking the filters over replaces them with its own.
        "_ambient_q_count",
        # QuerySet's own: the relations to load with the model instances.
        "_prefetch_map",
        "_prefetch_queries",
        "_select_related",
        # Descriptions of keys starting with an annotation (see _get_annotation_description()),
        # shared with clones until one of them changes its annotations.
        "_annotation_descriptions",
        # What .values()/.values_list() select - the rows come back in its shape - or None for
        # model instances.
        "_selection",
        # The querysets union()/intersection()/difference() combine - the rows are the combined
        # ones - or None for a queryset of its own model's rows.
        "_combination",
    )

    # Settings kept in self._options (QueryOptions) - read and assigned like attributes. An assigned
    # collection is stored immutable, so change one by assigning a new value, never in place.
    # select_for_update() and its flags.
    _select_for_update = QueryOption[bool]("select_for_update")
    _select_for_update_nowait = QueryOption[bool]("select_for_update_nowait")
    _select_for_update_skip_locked = QueryOption[bool]("select_for_update_skip_locked")
    _select_for_update_of = QueryOption[frozenset[str]]("select_for_update_of")
    _select_for_update_strength = QueryOption[Any]("select_for_update_strength")
    # The keyset boundary .after_cursor() set: rows strictly after these values in `_orderings`.
    _cursor_values = QueryOption[tuple[Any, ...]]("cursor_values")
    # The second keyset boundary: rows strictly BEFORE these values in `_orderings` - set
    # only when both .after_cursor() and .before_cursor() bound one query (a window).
    _before_cursor_values = QueryOption[tuple[Any, ...]]("before_cursor_values")
    # Set by .before_cursor(): `_orderings` already holds the reversed ordering (so LIMIT
    # counts back from the cursor), and the fetched rows are reversed back afterwards.
    _reverse_result_order = QueryOption[bool]("reverse_result_order")
    # (name, query) pairs populated by QuerySet.with_cte() and threaded through to count()/
    # exists()/values()/values_list(), the same way _group_bys is - see QueryCtes.apply_with_ctes().
    _with_ctes = QueryOption[tuple[tuple[str, Any], ...]]("with_ctes")
    # Calls of QuerySet methods a dialect registered (QuerySetExtensions), applied once the
    # query is built for its connection.
    _extension_calls = QueryOption[tuple[QuerySetExtensionCall, ...]]("extension_calls")
    # Select(relation, extra_condition=Q(...))'s condition per relation path.
    _select_related_extra_conditions = QueryOption[Mapping[str, Q]]("select_related_extra_conditions")
    # The fields of .distinct(*fields) (DISTINCT ON).
    _distinct_on = QueryOption[tuple[str, ...]]("distinct_on")
    # The fields of .group_by().
    _group_bys = QueryOption[tuple[str, ...]]("group_bys")
    # The Rollup/Cube/GroupingSets of .group_by() - its fields are in _group_bys too.
    _grouping_set = QueryOption[Any]("grouping_set")
    # The sample of the model's table .sample() reads (a TableSample) - None for every row.
    _table_sample = QueryOption[Any]("table_sample")
    # The `_annotations` keys registered by .alias(): usable in filter()/order_by(), selected only
    # when .values()/.values_list() names them.
    _alias_keys = QueryOption[frozenset[str]]("alias_keys")
    # The exception .get(..., does_not_exist_exception=...) raises for no row instead of
    # DoesNotExist - a class or an instance.
    _does_not_exist_exception = QueryOption[type[BaseException] | BaseException | None]("does_not_exist_exception")
    # The exception .get(..., multiple_objects_returned_exception=...) raises for more than one row
    # instead of MultipleObjectsReturned - a class or an instance.
    _multiple_objects_returned_exception = QueryOption[type[BaseException] | BaseException | None](
        "multiple_objects_returned_exception"
    )
    # Set when a single-row query (first(), an index) narrows an already sliced query - its row
    # is picked from the slice, so it can't be filtered or reordered any more.
    _is_single_row_of_slice = QueryOption[bool]("is_single_row_of_slice")
    # Whether Meta.ordering is left out when no ordering is given - set by order_by() with no
    # arguments, and on a branch of a set operation, where only the combined rows can be ordered.
    _default_ordering_disabled = QueryOption[bool]("default_ordering_disabled")

    # The .only() field names.
    _fields_for_select = QueryOption[tuple[str, ...]]("fields_for_select")
    # The .defer() field names.
    _deferred_fields = QueryOption[tuple[str, ...]]("deferred_fields")
    # Relations whose field-level lazy= default .defer_related() switched off.
    _deferred_related_fields = QueryOption[frozenset[str]]("deferred_related_fields")
    # The calls kept before Hare.init() set the model up.
    _calls_before_setup = QueryOption[CallsBeforeSetup | None]("calls_before_setup")
    # Relations named by an explicit .select_related(...) call, not joined by a field-level
    # lazy="joined" default - only an explicit one is fetched whatever .only() lists.
    _explicitly_select_related = QueryOption[frozenset[str]]("explicitly_select_related")

    #: The slots a query built from a specification takes over, in the order it copies them.
    SPECIFICATION_SLOTS: ClassVar[tuple[str, ...]] = __slots__
    #: The specification slots holding a container a build changes - a query gets its own copy -
    #: each with the source of an empty container of its type, made instead of copying an empty one.
    MUTABLE_SPECIFICATION_SLOTS: ClassVar[dict[str, str]] = {
        "_prefetch_map": "{}",
        "_orderings": "[]",
        "_q_object_list": "[]",
        "_annotations": "{}",
        "_select_related": "set()",
    }
    #: How each slot meets a plan key - every slot is named, checked when the module loads. A slot
    #: holding SQL that no plan key reads would let two queries of different SQL share a plan.
    SLOT_PLAN_ROLES: ClassVar[dict[str, SpecificationSlotPlanRole]] = {
        "model": SpecificationSlotPlanRole.KEYED,
        "_connection": SpecificationSlotPlanRole.CONNECTION,
        "_features": SpecificationSlotPlanRole.CONNECTION,
        "_connection_explicitly_chosen": SpecificationSlotPlanRole.CONNECTION,
        "_router_fallback_connection": SpecificationSlotPlanRole.CONNECTION,
        "_instance_connection_alias": SpecificationSlotPlanRole.CONNECTION,
        "_annotations": SpecificationSlotPlanRole.DESCRIBED,
        "_q_object_list": SpecificationSlotPlanRole.DESCRIBED,
        "_filter_call_counter": SpecificationSlotPlanRole.DESCRIBED,
        "_pending_filter_calls": SpecificationSlotPlanRole.DESCRIBED,
        "_single": SpecificationSlotPlanRole.KEYED,
        "_raise_does_not_exist": SpecificationSlotPlanRole.RESULT,
        "_limit": SpecificationSlotPlanRole.KEYED,
        "_offset": SpecificationSlotPlanRole.KEYED,
        "_orderings": SpecificationSlotPlanRole.KEYED,
        "_distinct": SpecificationSlotPlanRole.KEYED,
        "_is_none": SpecificationSlotPlanRole.KEYED,
        "_uses_default_scope": SpecificationSlotPlanRole.DESCRIBED,
        "_visibility": SpecificationSlotPlanRole.KEYED,
        "_options": SpecificationSlotPlanRole.KEYED,
        "_ambient_q_count": SpecificationSlotPlanRole.BOOKKEEPING,
        "_prefetch_map": SpecificationSlotPlanRole.RESULT,
        "_prefetch_queries": SpecificationSlotPlanRole.RESULT,
        "_select_related": SpecificationSlotPlanRole.KEYED,
        "_annotation_descriptions": SpecificationSlotPlanRole.BOOKKEEPING,
        "_selection": SpecificationSlotPlanRole.KEYED,
        "_combination": SpecificationSlotPlanRole.KEYED,
    }

    @classmethod
    def raise_if_slots_unclassified(cls) -> None:
        """Rejects a slot of ``SPECIFICATION_SLOTS`` without its role in ``SLOT_PLAN_ROLES``, and a role of no slot.

        Raises:
            TypeError: The two disagree.
        """
        if set(cls.SLOT_PLAN_ROLES) != set(cls.SPECIFICATION_SLOTS):
            raise TypeError(
                "QuerySpecification.SLOT_PLAN_ROLES names every slot of SPECIFICATION_SLOTS and no other - "
                "unclassified: "
                f"{sorted(set(cls.SPECIFICATION_SLOTS) - set(cls.SLOT_PLAN_ROLES))}, unknown: "
                f"{sorted(set(cls.SLOT_PLAN_ROLES) - set(cls.SPECIFICATION_SLOTS))}"
            )

    def __init__(self, model: type[TModel]) -> None:
        self.model: type[TModel] = model
        self._options: QueryOptions = QueryOptions.DEFAULT
        self._connection: DatabaseClient = None  # type: ignore[assignment]
        self._features: Features | None = None
        self._connection_explicitly_chosen: bool = False
        self._router_fallback_connection: DatabaseClient | None = None
        self._instance_connection_alias: str | None = None
        self._annotations: dict[str, Expression | Term] = {}
        self._q_object_list: list[Q] = []
        #: The filter calls kept unbuilt: (is exclude(), generation, Q conditions, kwargs) each.
        self._pending_filter_calls: tuple[tuple[bool, int, tuple[Q, ...], dict[str, Any]], ...] = ()
        self._filter_call_counter: int = 0
        self._single: bool = False
        self._raise_does_not_exist: bool = False
        self._limit: int | None = None
        self._offset: int | None = None
        self._orderings: Sequence[tuple[str, Order]] = []
        self._distinct: bool = False
        self._is_none: bool = False
        self._visibility: RowVisibility = RowVisibility.DEFAULT
        self._uses_default_scope: bool = False
        self._ambient_q_count: int = 0
        self._prefetch_map: dict[str, set[str | Prefetch]] = {}
        self._prefetch_queries: dict[str, list[tuple[str | None, QuerySet[Model]]]] = {}
        self._select_related: set[str] = set()
        self._annotation_descriptions: dict[tuple[str, str, str], Any] | None = None
        self._selection: ValuesSelection | None = None
        self._combination: Combination | None = None

    @property
    def _q_objects(self) -> list[Q]:
        """The conditions the query filters by - the filter calls a queryset made by simple calls
        alone keeps unbuilt (``_pending_filter_calls``) are built on the first read."""
        if self._pending_filter_calls:
            PendingFilterCalls.build_pending_filter_calls(self)
        return self._q_object_list

    @_q_objects.setter
    def _q_objects(self, q_objects: list[Q]) -> None:
        # The conditions given replace every condition, built or not.
        self._pending_filter_calls = ()
        self._q_object_list = q_objects

    def _build_conditions_for_copies(self) -> None:
        """Builds the filter calls kept unbuilt before the query is copied for a description or a
        build of a query it is part of - every copy then holds the same conditions, the origins of
        their values (``PlanOrigins``)."""
        if self._pending_filter_calls:
            PendingFilterCalls.build_pending_filter_calls(self)

    def _apply_lazy_relation_defaults(self) -> None:
        """Adds the relations the model loads by default (``lazy=``) and ``.defer_related()``
        didn't switch off to the ones this query joins or prefetches - on the copy a build makes,
        before it reads them."""
        joined, prefetched = LazyRelationNames.get(self.model)
        self._select_related.update(joined - self._deferred_related_fields)
        for field_name in prefetched - self._deferred_related_fields:
            self._prefetch_map.setdefault(field_name, set())

    def _model_is_set_up(self) -> bool:
        """Whether ``Hare.init()`` has set the model up - its relations resolved. A query built
        before that checks its field names, relations and lookups when it is compiled instead of
        when each method is called."""
        return self.model._meta._inited

    def _loads_relations(self) -> bool:
        """Whether reading this query's rows loads relations - asked for, or by default when the
        rows are model instances."""
        if self._select_related or self._prefetch_map or self._prefetch_queries:
            return True
        if self._selection is not None:
            return False
        joined, prefetched = LazyRelationNames.get(self.model)
        return bool((joined | prefetched) - self._deferred_related_fields)

    def _take_rows_of(
        self, source: QuerySpecification[Any], *, keeps_ordering: bool = False, keeps_grouping: bool = False
    ) -> None:
        """Takes over which rows ``source`` matches - its model, connection choice, default scope,
        filters, annotations, ``.distinct()``, slice, keyset boundaries, CTEs and dialect method
        calls - and nothing of how it returns them.

        Args:
            source: The queryset, or a query made from one.
            keeps_ordering: Keep the ordering - a slice is taken in it. Without it the ordering is
                kept only for the keyset boundaries.
            keeps_grouping: Keep the ``.group_by()`` fields.
        """
        SpecificationCopying.copy_specification(source, self)
        self._single = False
        self._raise_does_not_exist = False
        self._prefetch_map = {}
        self._prefetch_queries = {}
        self._select_related = set()
        self._annotation_descriptions = None
        self._selection = None
        self._combination = None
        options = self._options
        if options is not QueryOptions.DEFAULT:
            options = options.without(*QueryOptions.ROW_RETURN_SETTINGS)
            self._options = options if keeps_grouping else options.without("group_bys")
            if not keeps_ordering and not (options.cursor_values or options.before_cursor_values):
                self._orderings = []
        elif not keeps_ordering:
            self._orderings = []

    @staticmethod
    def _raise_if_annotation_names_are_unsafe(names: Iterable[str]) -> None:
        """Rejects an annotation, alias or aggregate name with quotes, brackets, a semicolon,
        whitespace or an SQL comment - an output column name taken from user input stays a plain
        name, like Django's own check.

        Args:
            names: The new names.

        Raises:
            ValueError: A name contains one of them.
        """
        for name in names:
            # An identifier - most names - holds none of them.
            if not name.isidentifier() and FORBIDDEN_ANNOTATION_NAME_PATTERN.search(name):
                raise QueryError(
                    f"Annotation name {name!r} can't contain whitespace, quotation marks, brackets, "
                    "semicolons or SQL comments"
                )

    @property
    def features(self) -> Features:
        """What the connection the query runs on supports.

        Raises:
            QueryError: No connection is chosen yet - the query isn't running.
        """
        if self._features is None:
            self._features = QueryConnection.get_bound_connection(self).features
        return self._features

    @property
    def dialect(self) -> Dialect:
        """The dialect of the connection the query runs on.

        Raises:
            QueryError: No connection is chosen yet - the query isn't running.
        """
        return QueryConnection.get_bound_connection(self).dialect

    def _get_top_level_conditions_by_generation(self) -> dict[int, list[tuple[str, Any]]]:
        """The filter kwargs every kept row satisfies, per ``.filter()`` call generation.

        Returns:
            Generation -> key and value of each kwarg.
        """
        conditions_by_generation: dict[int, list[tuple[str, Any]]] = {}
        for q_object in self._q_objects:
            conditions_by_generation.setdefault(q_object._filter_call_generation, []).extend(
                self._get_top_level_conditions(q_object)
            )
        return conditions_by_generation

    def _get_probe_expression_context(
        self,
        annotations: dict[str, Any],
        aggregated_multi_valued_paths: AggregatedMultiValuedPaths | None = None,
        multi_valued_join_generations: dict[str, int] | None = None,
    ) -> ExpressionContext:
        """Builds a throwaway context over ``annotations``, used only to find out which annotations or
        to-many relations an expression or a filter reads. Before the query runs it resolves with
        the neutral SQL dialect.

        Args:
            annotations: The annotation mapping expressions are resolved against.
            aggregated_multi_valued_paths: A tracker recording the to-many JOINs made.
            multi_valued_join_generations: Collects the ``.filter()`` call generation owning each
                to-many JOIN.

        Returns:
            A fresh context with the scope the real query resolves in.
        """
        return ExpressionContext(
            model=self.model,
            dialect=QueryConnection.get_analysis_dialect(self),
            connection=self._connection,
            table=self._effective_basetable(),
            annotations=annotations,
            aggregated_multi_valued_paths=aggregated_multi_valued_paths,
            multi_valued_join_generations=multi_valued_join_generations,
            select_related_extra_conditions=self._select_related_extra_conditions,
            visibility=self._visibility,
            # Building the real query rejects a filter on a window function where SQL can't apply it.
            window_function_filter_allowed=True,
        )

    @staticmethod
    def _get_ordering_string(ordering: str | Ordering, reverse: bool = False) -> tuple[str, Order]:
        """Splits one ordering item into its field name and direction.

        Args:
            ordering: A ``"field"``/``"-field"`` string, or an ``Ordering`` (``F("field").desc(nulls_last=True)``).
            reverse: Whether to reverse the ordering - direction and any explicit NULL placement.

        Raises:
            QueryError: If the item is neither a string nor an ``Ordering``.
        """
        if isinstance(ordering, Ordering):
            field_name = ordering.field_name
            order_type = ordering.order
        elif isinstance(ordering, str):
            if ordering[0] == "-":
                field_name = ordering[1:]
                order_type = Order.DESC
            else:
                field_name = ordering
                order_type = Order.ASC
        else:
            raise QueryError(
                f"An ordering must be a field name string or an Ordering (F('field').asc()/.desc()), got {ordering!r}"
            )

        if reverse:
            order_type = order_type.get_reversed()

        return field_name, order_type

    def _apply_default_ordering(
        self,
        orderings: Iterable[tuple[str, Order]],
        annotations: dict[str, Term | Expression],
    ) -> Iterable[tuple[str, Order]]:
        """Falls back to ``Meta.ordering`` when no ordering was given. Skipped when an annotation may
        make the query group implicitly - the ordering columns needn't be grouped then - and when
        the default ordering is disabled.
        """
        if (
            not orderings
            and not self._default_ordering_disabled
            and self.model._meta.ordering
            and all(
                isinstance(annotation, Expression) and RowMultiplication.annotation_is_group_by_safe(annotation)
                for annotation in annotations.values()
            )
        ):
            return self.model._meta.ordering
        return orderings

    @staticmethod
    def _check_chunk_size(chunk_size: int) -> None:
        """Checks the page size of ``iterator()`` / ``stream()``.

        Args:
            chunk_size: The page size.

        Raises:
            QueryError: ``chunk_size`` isn't positive.
        """
        if chunk_size <= 0:
            raise QueryError("chunk_size must be a positive integer")

    @staticmethod
    def _check_streamable(connection: DatabaseClient | None) -> DatabaseClient:
        """Checks that ``stream()`` can run on ``connection``: a server-side cursor / portal, inside a
        transaction - only one transaction gives it a consistent snapshot for the whole iteration;
        outside one on a database sending a query's rows as it computes them.

        Args:
            connection: The connection the query runs on.

        Returns:
            The connection - the transaction's client inside one.

        Raises:
            UnSupportedError: The database has no streaming (``features.supports_streaming``).
            QueryError: No transaction is open, on a database streaming inside one alone.
        """
        connection = cast("DatabaseClient", connection)
        if not connection.features.supports_streaming:
            raise UnSupportedError(
                f"stream() is not supported on the {connection.dialect.name} backend - it "
                "has no useful server-side row streaming to offer; use iterator() instead"
            )
        if not connection.is_transaction_client and not connection.features.streams_without_transaction:
            raise QueryError(
                "stream() requires an active Transactions.atomic() block - its server-side "
                "cursor/portal only gets a consistent snapshot for the whole iteration when scoped "
                "to one transaction"
            )
        return connection

    @staticmethod
    def _get_sliced_bounds(key: slice, base_offset: int | None, base_limit: int | None) -> tuple[int, int | None]:
        """The OFFSET and LIMIT of a query sliced by ``key`` - the slice taken within the query's
        own OFFSET and LIMIT.

        Args:
            key: The slice.
            base_offset: The query's own OFFSET.
            base_limit: The query's own LIMIT, None for none.

        Returns:
            The OFFSET and the LIMIT, None for no LIMIT.

        Raises:
            QueryError: A step other than 1, or a start or stop that isn't a non-negative integer.
        """
        if not (key.step is None or (isinstance(key.step, int) and key.step == 1)):
            raise QueryError("Slice steps should be 1 or None.")
        start = key.start if key.start is not None else 0
        if not isinstance(start, int) or start < 0:
            raise QueryError("Slice start should be non-negative number or None.")
        if key.stop is not None and (not isinstance(key.stop, int) or key.stop < 0):
            raise QueryError("Slice stop should be non-negative number or None.")
        base_offset = base_offset or 0
        offset = base_offset + start
        stop: int | None
        if key.stop is not None:
            stop = base_offset + key.stop
            if base_limit is not None:
                stop = min(stop, base_offset + base_limit)
        else:
            stop = base_offset + base_limit if base_limit is not None else None
        return offset, (max(stop - offset, 0) if stop is not None else None)

    def _share_default_scope(self, query: TDerivedAwaitableQuery) -> TDerivedAwaitableQuery:
        """Hands this query's default scope to a query built from its filters.

        Args:
            query: The derived query.

        Returns:
            ``query`` itself.
        """
        query._uses_default_scope = self._uses_default_scope
        query._ambient_q_count = self._ambient_q_count
        query._visibility = self._visibility
        query._options = query._options.updated(extension_calls=self._extension_calls)
        return query

    def _effective_basetable(self) -> Table:
        """The table this query's own fields and filters are read from: the model's table, aliased when
        the query is a correlated subquery over the table its enclosing query selects from - an
        ``OuterReference`` would otherwise bind to this query's own row.
        """

        table = self.model._meta.basetable
        outer_context = outer_expression_context.get()
        if outer_context is not None and table.get_table_name() == outer_context.table.get_table_name():
            return table.as_(Identifiers.get_within_limit(f"{table.get_table_name()}__exists_inner"))
        return table

    @classmethod
    def _get_top_level_conditions(cls, q_object: Q, including_alternatives: bool = False) -> list[tuple[str, Any]]:
        """The kwargs of a filter every kept row satisfies - ANDed, not negated. A negated node,
        and a node negated twice, crossing a relation renders as a ``NOT EXISTS``/``EXISTS``
        subquery instead of a JOIN of the query, so its kwargs are never included.

        Args:
            q_object: The filter.
            including_alternatives: Also the kwargs of OR-ed nodes.

        Returns:
            Key and value of each kwarg.
        """
        if q_object._is_negated or q_object._needs_double_negation_rewrite:
            return []
        if not including_alternatives and q_object.connector != Connector.AND:
            return []
        conditions = list(q_object.filters.items())
        for child in q_object.children:
            conditions.extend(cls._get_top_level_conditions(child, including_alternatives))
        return conditions

    def _get_selected_field_names(self) -> Collection[str]:
        """The field/annotation names a ``values()``/``values_list()`` query selects.

        Returns:
            The names, empty for a query that selects whole rows.
        """
        return ()

    def _get_unused_alias_keys(self, fields_for_select: Collection[str] | None = None) -> set[str]:
        """The ``.alias()`` annotations nothing in the query reads - left out of the query
        entirely, so their JOINs don't repeat rows.

        Args:
            fields_for_select: The names selected explicitly, if any.

        Returns:
            The unused alias names.
        """
        if not self._alias_keys:
            return set()
        directly_used_names = {
            *(fields_for_select or ()),
            *self._get_selected_field_names(),
            *self._group_bys,
            *self._distinct_on,
            *(field_name for field_name, _order in self._orderings),
        }
        tracker = AnnotationAccessTracker(self._annotations)
        for annotation_name, annotation in self._annotations.items():
            if annotation_name in self._alias_keys and annotation_name not in directly_used_names:
                continue
            if isinstance(annotation, Expression):
                annotation.get_result(self._get_probe_expression_context(tracker))
        for q_object in self._q_objects:
            q_object.get_result(self._get_probe_expression_context(tracker))
        return {
            alias_key
            for alias_key in self._alias_keys
            if alias_key not in directly_used_names and alias_key not in tracker.accessed_keys
        }

    # Declared for the type checker: _apply_connection() assigns None.
    _features: Features | None

    # Declared for the type checker: the slot of the queryset and query classes, set only on a
    # query made again for each description or build (PlanOrigins).
    _plan_origin: Any

    # Declared here for the same reason as _db/_features above - get_connection() below (this
    # class, not a subclass) reads it directly.
    _router_fallback_connection: DatabaseClient | None

    _instance_connection_alias: str | None

    _visibility: RowVisibility

    model: type[TModel]

    #: Whether the query takes the QuerySet methods dialects registered (QuerySetExtensions).
    accepts_extension_methods: ClassVar[bool] = False

    #: Whether the query binds the values of an annotation a GROUP BY, ORDER BY or DISTINCT ON
    #: resolves again into its full expression (QueryAnnotations.get_expression_term_value_references()) - a query of
    #: another type ordering by such an annotation keeps no plan.
    binds_expression_term_values: ClassVar[bool] = False

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any:
            # A QuerySet method a dialect registered (QuerySetExtensions).
            if self.accepts_extension_methods and name in QuerySetExtensions.registered:
                return QuerySetExtensions.get_method(cast("QuerySet[Any]", self), name)
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    # The slot names of the class, cached per subclass.
    clone_slots_cache: ClassVar[tuple[str, ...] | None] = None

    # The compiled per-subclass copy function SpecificationCopying.compile_copy() builds - see its own docstring.
    compiled_copy_cache: ClassVar[Callable[[Any, Any], None] | None] = None

    # The slots holding a mutable container, each with the source of an empty container of its type
    # - a clone gets its own shallow copy.
    mutable_clone_slots: ClassVar[dict[str, str]] = {}

    def __copy__(self) -> QuerySpecification[TModel]:
        cls = type(self)
        newone = cls.__new__(cls)
        (cls.__dict__.get("compiled_copy_cache") or SpecificationCopying.compile_copy(cls))(self, newone)
        # A subclass without __slots__ keeps its own attributes in a __dict__ - asked of the class:
        # hasattr() on a slotted instance would raise through __getattr__().
        if cls.__dictoffset__:
            newone.__dict__.update(self.__dict__)
        return newone

    def get_connection(self, *, for_write: bool = False) -> DatabaseClient:
        """Returns the connection this query would run on now: the one pinned with ``using()``, else
        the router's choice (asked under the tenant the query was built for), else the connection of
        the instance a relation was read off, else the one a parent queryset offered to its
        prefetch, else the model's default. Inside an open transaction on it, the transaction's
        client. Nothing is bound - pass the result to ``using()`` to pin it.

        Args:
            for_write: Whether the connection is chosen for a write.

        Returns:
            The chosen connection.

        Raises:
            ConfigurationError: No connection is pinned and no Hare context is active; the model has
                no default connection, or its tenancy needs a setting of the connection it lacks
                (``MetaInfo.check_tenant_client()``).
            QueryError: The model's tenancy needs a single active tenant, or a transaction that set
                its tenants (``MetaInfo.check_tenant_client()``).
        """
        if self._connection:
            return self._connection
        # HareContext.require_current() - its calls left for the error.
        context = HareContext.current_context.get() or HareContext.global_context or HareContext.require_current()
        # The connection is chosen under the tenant this query's WHERE was built for, not the one
        # active when the query is awaited - a router routing by tenant, or a connection with a schema
        # per tenant, would otherwise pick another tenant's database or schema.
        tenant_snapshot = self._visibility.tenant
        tenant_token = (
            None
            if tenant_snapshot is AMBIENT_TENANT_NOT_OVERRIDDEN
            or tenant_snapshot is None
            or tenant_snapshot is Tenancy.current.get()
            else Tenancy.current.set(tenant_snapshot)
        )
        # One method, not two: this is the hottest path of the ORM, and a try costs nothing until
        # something raises.
        try:
            # HareContext.router/connections and ConnectionRouter.db_for_read/write() when there are
            # routers - read without their calls.
            router = context._router
            connection = (
                (router.db_for_write(self.model) if for_write else router.db_for_read(self.model))
                if router is not None and router._routers
                else None
            )
            meta = self.model._meta
            if connection is None:
                connections = context._connections if context._connections is not None else context.connections
                if self._instance_connection_alias is not None:
                    connection = connections.get(self._instance_connection_alias)
                elif self._router_fallback_connection is not None:
                    connection = self._router_fallback_connection
                else:
                    # The model's default connection, read from the context already in hand.
                    default_connection = meta.default_connection
                    if default_connection is None:
                        raise ConfigurationError(f"default_connection for the model {self.model} cannot be None")
                    connection = connections.get(default_connection)
            if meta.checks_tenant_client:
                meta.check_tenant_client(connection)
            return connection
        finally:
            if tenant_token is not None:
                Tenancy.current.reset(tenant_token)

    def _apply_connection(self, connection: DatabaseClient | None) -> None:
        """Binds this query to the connection it runs on.

        Args:
            connection: The database connection to use for this query.
        """
        # None leaves the connection to be chosen when the query runs.
        self._connection = connection  # type: ignore[assignment]
        self._features = None

    def _get_base_query(self) -> QueryBuilder:
        """A fresh ``SELECT ... FROM`` the model's table, built for the connection the query runs on.

        Returns:
            The query builder.
        """
        meta = self.model._meta
        connection = self._connection if self._connection is not None else meta.connection
        if connection.query_class is meta.connection.query_class:
            return self._get_sampled_query(copy(meta.basequery), connection)
        return self._get_sampled_query(connection.query_class.from_(meta.basetable), connection)

    def _get_base_query_all_fields(self) -> QueryBuilder:
        """``_get_base_query()`` selecting every column of the model.

        Returns:
            The query builder.
        """
        meta = self.model._meta
        connection = self._connection if self._connection is not None else meta.connection
        if connection.query_class is meta.connection.query_class:
            return self._get_sampled_query(copy(meta.basequery_all_fields), connection)
        return self._get_sampled_query(
            connection.query_class.from_(meta.basetable).select(*meta.db_fields), connection
        )

    def _get_sampled_query(self, query: QueryBuilder, connection: DatabaseClient) -> QueryBuilder:
        """``query`` reading the sample of the model's table ``sample()`` asked for.

        Args:
            query: A ``SELECT ... FROM`` the model's table.
            connection: The connection the query runs on.

        Returns:
            The query - itself without a sample.

        Raises:
            UnSupportedError: The database can't read a sample of a table.
            QueryError: The model's table options allow no sample (``TableOptions.raise_if_unsampled()``).
        """
        table_sample = self._table_sample
        if table_sample is None:
            return query
        if not connection.features.supports_table_sample:
            raise UnSupportedError(f"sample() needs TABLESAMPLE, which {connection.dialect} doesn't have")
        table_options = self.model._meta.get_table_options(connection.dialect)
        if table_options is not None:
            table_options.raise_if_unsampled(self.model)
        return query.sample(table_sample.method, table_sample.percent, table_sample.seed)

    def _get_execution_query(self, for_write: bool = False) -> Self:
        """Returns the query to run now: itself when its connection is pinned, otherwise a copy
        bound to the connection chosen for this one execution, so a queryset evaluated again later
        (after a transaction ends, inside another one, from another task) chooses afresh.

        Args:
            for_write: Whether the connection is chosen for a write.

        Returns:
            The query bound to a connection.
        """
        if cast("DatabaseClient | None", self._connection) is not None:
            return self
        execution_query = copy(self)
        execution_query._apply_connection(execution_query.get_connection(for_write=for_write))
        return execution_query

    def _get_compiler(self) -> QuerySpecification[Any]:
        """The query building this query's SQL - the query itself, unless it only describes the
        rows another query builds.

        Returns:
            The query.
        """
        return self

    def _make_query(self) -> None:
        """Builds the query for the connection it runs on."""
        raise NotImplementedError()  # pragma: nocoverage

    def _get_statements(self, parameters_inline: bool) -> list[tuple[str, list[Any]]]:
        """The statements the query runs, built for the connection it runs on.

        Args:
            parameters_inline: Whether values are rendered into the SQL instead of bound.

        Returns:
            ``(sql, bound values)`` of each statement.
        """
        raise NotImplementedError()  # pragma: nocoverage

    def _get_explain_statements(
        self, statements: list[tuple[str, list[Any]]], output_format: str | None, options: dict[str, bool]
    ) -> list[tuple[str, list[Any]]]:
        """The EXPLAIN statement of each statement, in the dialect of the connection the query runs on.

        Args:
            statements: ``(sql, bound values)`` of each explained statement.
            output_format: The plan's output format, None for the dialect's default.
            options: The EXPLAIN options.

        Returns:
            ``(sql, bound values)`` of each EXPLAIN statement.
        """
        connection = self._connection
        clauses = connection.dialect.clauses
        return [
            (clauses.get_explain_sql(sql, output_format, options, connection.features), values)
            for sql, values in statements
        ]

    def sql(
        self,
        parameters_inline: bool = False,
        *,
        explain: bool = False,
        output_format: str | None = None,
        **explain_options: bool,
    ) -> str:
        """The SQL the query runs, in the dialect of the connection it runs on.

        Args:
            parameters_inline: Whether values are rendered into the SQL instead of placeholders.
            explain: Return the ``EXPLAIN`` statement of the query instead - what ``explain()`` runs.
            output_format: The plan's output format with ``explain=True``.
            explain_options: The EXPLAIN options with ``explain=True``.

        Returns:
            The SQL; several statements are joined with ``;``.

        Raises:
            QueryError: ``output_format`` or an EXPLAIN option is given without ``explain=True``.
            UnSupportedError: The dialect has no such EXPLAIN format or option.
        """
        if not explain and (output_format is not None or explain_options):
            raise QueryError("output_format and EXPLAIN options apply only with explain=True")
        execution_query = self._get_compiler()._get_execution_query()
        statements = execution_query._get_statements(parameters_inline)
        if explain:
            statements = execution_query._get_explain_statements(statements, output_format, explain_options)
        return ";".join(sql for sql, _values in statements)

    async def explain(self, output_format: str | None = None, **options: bool) -> list[Any]:
        """Runs the query's ``EXPLAIN`` statement - the one ``sql(explain=True)`` returns - and
        returns the plan rows.

        Args:
            output_format: The plan's output format.
                - PostgreSQL: ``text``, ``json``, ``xml``, ``yaml`` (default: ``json``)
                - SQLite: none
            options: The EXPLAIN options.
                - PostgreSQL: ``analyze``, ``buffers``, ``costs``, ``memory``, ``settings``,
                  ``summary``, ``timing``, ``verbose``, ``wal``, ``generic_plan``, ``serialize``
                  (default: ``verbose``)
                - SQLite: none

        Returns:
            The plan rows; their shape depends on the database.

        Raises:
            UnSupportedError: The dialect has no such format or option.
        """
        execution_query = self._get_compiler()._get_execution_query()
        connection = execution_query._connection
        statements = execution_query._get_explain_statements(
            execution_query._get_statements(parameters_inline=False), output_format, options
        )
        rows: list[Any] = []
        for sql, values in statements:
            rows.extend((await connection.execute(sql, values))[1])
        return rows


# The query classes are defined above, and the expressions imported - a value of any of them is
# written into a statement or a subquery, never bound as a parameter.
StatementPlan.unbindable_value_classes = (Term, Expression, QuerySpecification)
QuerySpecification.raise_if_slots_unclassified()
