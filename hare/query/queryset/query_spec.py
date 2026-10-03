from __future__ import annotations

from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Generic, NoReturn, Self, TypeVar, cast

from hare.core.cache import Cache
from hare.core.lookup_path import LookupPath
from hare.core.model_cache import ModelCache
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.constants import SQL_DIALECT
from hare.dialects.base.dialect import Dialect
from hare.dialects.base.features import Features
from hare.dialects.identifiers import Identifiers
from hare.exceptions import (
    ConfigurationError,
    DoesNotExist,
    QueryError,
    UnSupportedError,
)
from hare.fields.enums import RelationLoadStrategy
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.models.tenancy import Tenancy
from hare.query.composite import KeyColumns
from hare.query.constants import (
    AMBIENT_TENANT_NOT_OVERRIDDEN,
    FORBIDDEN_ANNOTATION_NAME_PATTERN,
)
from hare.query.enums import Connector, Lookup
from hare.query.expressions import (
    CombinedExpression,
    Expression,
    ExpressionContext,
    F,
    Ordering,
    Q,
    Value,
)
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.exists import Exists
from hare.query.expressions.outer_query_state import outer_expression_context
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.statement_plan import StatementPlan
from hare.query.queryset.calls_before_setup import CallsBeforeSetup
from hare.query.queryset.extensions.query_set_extension_call import QuerySetExtensionCall
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions
from hare.query.queryset.query_option import QueryOption
from hare.query.queryset.query_options import QueryOptions
from hare.query.relation_loading.prefetch import Prefetch
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql import Order, Table
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.queries.tables.selectable import Selectable
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
    from hare.models import Model
    from hare.query.queryset.combination import Combination
    from hare.query.queryset.direct_get import DirectGet
    from hare.query.queryset.queryset import QuerySet
    from hare.query.queryset.values_selection import ValuesSelection

TModel = TypeVar("TModel", bound="Model")

TDerivedAwaitableQuery = TypeVar("TDerivedAwaitableQuery", bound="QuerySpec[Any]")


class QuerySpec(Plannable, Generic[TModel], abstract=True):
    """What a query asks for - the model, the connection choice, the filters, annotations, ordering,
    slice and the other settings the chained methods declare. ``QuerySet`` adds the methods changing
    it; an ``AwaitableQuery`` takes one over, builds and runs its SQL.
    """

    #: A model's field paths, in a bucket the model keeps: path -> the field paths it reads
    #: (``get_concrete_field_paths()``). A path crosses relations into other models, so a change
    #: to any model drops them all.
    concrete_field_paths: ClassVar[Cache[Any]] = Cache(
        holds_sql=False, keyed_by_model=False, depends_on_other_models=True, model_attribute="concrete_field_paths"
    )

    class AnnotationAccessTracker(dict[str, Any]):
        """Annotation mapping that remembers which keys expression resolution actually read."""

        def __init__(self, annotations: dict[str, Any]) -> None:
            super().__init__(annotations)
            self.accessed_keys: set[str] = set()

        def __getitem__(self, key: str) -> Any:
            self.accessed_keys.add(key)
            return super().__getitem__(key)

        def get(self, key: str, default: Any = None) -> Any:
            if key in self:
                self.accessed_keys.add(key)
            return super().get(key, default)

    __slots__ = (
        "model",
        "_db",
        "_features",
        # Whether .using() pinned the connection - _db is also set once the router or the default
        # connection chose it. A pinned connection is offered to the prefetch queries.
        "_db_explicitly_chosen",
        # The connection the parent queryset pinned with .using(), offered to its prefetch query:
        # taken after the router and before the model's default connection.
        "_router_fallback_db",
        # Set by a relation read off a model instance: the alias of the connection the instance was
        # loaded from or saved to - taken after the router and before the model's default
        # connection, looked up when the query runs.
        "_instance_connection_name",
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
        # Set by get()/get_or_none() with plain filters on a queryset nothing else changed - see
        # QuerySet._get_direct_get().
        "_direct_get",
    )

    # Settings kept in self._options (QueryOptions) - read and assigned like attributes. An assigned
    # collection is stored immutable, so change one by assigning a new value, never in place.
    # select_for_update() and its flags.
    _select_for_update = QueryOption[bool]("select_for_update")
    _select_for_update_nowait = QueryOption[bool]("select_for_update_nowait")
    _select_for_update_skip_locked = QueryOption[bool]("select_for_update_skip_locked")
    _select_for_update_of = QueryOption[frozenset[str]]("select_for_update_of")
    _select_for_update_no_key = QueryOption[bool]("select_for_update_no_key")
    # The keyset boundary .after_cursor() set: rows strictly after these values in `_orderings`.
    _cursor_values = QueryOption[tuple[Any, ...]]("cursor_values")
    # The second keyset boundary: rows strictly BEFORE these values in `_orderings` - set
    # only when both .after_cursor() and .before_cursor() bound one query (a window).
    _before_cursor_values = QueryOption[tuple[Any, ...]]("before_cursor_values")
    # Set by .before_cursor(): `_orderings` already holds the reversed ordering (so LIMIT
    # counts back from the cursor), and the fetched rows are reversed back afterwards.
    _reverse_result_order = QueryOption[bool]("reverse_result_order")
    # (name, query) pairs populated by QuerySet.with_cte() and threaded through to count()/
    # exists()/values()/values_list(), the same way _group_bys is - see _apply_with_ctes().
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
    # The `_annotations` keys registered by .alias(): usable in filter()/order_by(), selected only
    # when .values()/.values_list() names them.
    _alias_keys = QueryOption[frozenset[str]]("alias_keys")
    # The exception .get(..., exception=...) raises for no row instead of DoesNotExist - a class or
    # an instance.
    _does_not_exist_exception = QueryOption[type[BaseException] | BaseException | None]("does_not_exist_exception")
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
    SPEC_SLOTS: ClassVar[tuple[str, ...]] = __slots__
    #: The specification slots holding a container a build changes - a query gets its own copy -
    #: each with the source of an empty container of its type, made instead of copying an empty one.
    MUTABLE_SPEC_SLOTS: ClassVar[dict[str, str]] = {
        "_prefetch_map": "{}",
        "_orderings": "[]",
        "_q_object_list": "[]",
        "_annotations": "{}",
        "_select_related": "set()",
    }
    spec_copy_cache: ClassVar[Callable[[Any, Any], None] | None] = None

    def __init__(self, model: type[TModel]) -> None:
        self.model: type[TModel] = model
        self._options: QueryOptions = QueryOptions.DEFAULT
        self._db: DatabaseClient = None  # type: ignore[assignment]
        self._features: Features | None = None
        self._db_explicitly_chosen: bool = False
        self._router_fallback_db: DatabaseClient | None = None
        self._instance_connection_name: str | None = None
        self._annotations: dict[str, Expression | Term] = {}
        self._q_object_list: list[Q] = []
        self._pending_filter_calls: tuple[tuple[bool, int, dict[str, Any]], ...] = ()
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
        self._direct_get: DirectGet | None = None

    @property
    def _q_objects(self) -> list[Q]:
        """The conditions the query filters by - the filter calls a queryset made by simple calls
        alone keeps unbuilt (``_pending_filter_calls``) are built on the first read."""
        if self._pending_filter_calls:
            self._build_pending_filter_calls()
        return self._q_object_list

    @_q_objects.setter
    def _q_objects(self, q_objects: list[Q]) -> None:
        # The conditions given replace every condition, built or not.
        self._pending_filter_calls = ()
        self._q_object_list = q_objects

    def _build_pending_filter_calls(self) -> None:
        """Builds the filter calls kept unbuilt into conditions, in call order."""
        pending_filter_calls = self._pending_filter_calls
        self._pending_filter_calls = ()
        # A copy of a query shares its conditions' list - the built ones go into a list of its own.
        self._q_object_list = list(self._q_object_list)
        for negate, generation, kwargs in pending_filter_calls:
            self._add_filter_conditions(negate, self._get_filter_kwarg_conditions(kwargs, generation), generation)

    def _get_filter_kwarg_conditions(self, kwargs: dict[str, Any], generation: int) -> list[Q]:
        """The conditions of the kwargs of one ``.filter()``/``.exclude()`` call.

        Args:
            kwargs: The filter kwargs.
            generation: The filter-call generation of the call.

        Returns:
            One condition per kwarg.
        """
        # A kwarg's Q is built right here and shared with nothing - stamped in place, not copied.
        return [
            self._build_filter_q(key, Q.get_list_lookup_value(key, value))._stamp_filter_call_generation(generation)
            for key, value in kwargs.items()
        ]

    def _add_filter_conditions(self, negate: bool, conditions: list[Q], generation: int) -> None:
        """Adds the conditions of one ``.filter()``/``.exclude()`` call to the query's conditions.

        Args:
            negate: Whether the call is ``exclude()``.
            conditions: Its conditions.
            generation: The filter-call generation of the call.
        """
        q_objects = self._q_objects
        if not negate:
            q_objects.extend(conditions)
        elif len(conditions) == 1:
            q_objects.append(~conditions[0])
        elif conditions:
            # Like Django, one exclude() call drops the rows matching ALL of its conditions:
            # NOT (a AND b), not NOT a AND NOT b.
            q_objects.append(~Q(*conditions)._with_filter_call_generation(generation))

    def _build_filter_q(self, key: str, value: Any) -> Q:
        """Builds the ``Q`` for one ``.filter()``/``.get()`` kwarg - a key over several fields
        (a composite primary key, ``relation__pk`` of a relation to one, a many-to-many relation
        to one) as comparisons of its fields, so each binds as a plain field value.

        Args:
            key: The filter kwarg.
            value: Its value.

        Returns:
            The ``Q``.
        """
        primary_key_q = KeyColumns.get_primary_key_q(self.model, key, value)
        if primary_key_q is not None:
            return primary_key_q
        for suffix, lookup in (("__pk__in", Lookup.IN), ("__pk", Lookup.EXACT)):
            if not key.endswith(suffix):
                continue
            relation_key = key[: -len(suffix)]
            related_model = getattr(self.model._meta.fields_map.get(relation_key), "related_model", None)
            if related_model is None or not isinstance(target_pk_attr := related_model._meta.pk_attr, tuple):
                break
            field_names = tuple(f"{relation_key}__{name}" for name in target_pk_attr)
            return KeyColumns.get_comparison_q(
                key, field_names, target_pk_attr, lookup, value, accepts_instances=False
            )
        field_object = self.model._meta.fields_map.get(key)
        if isinstance(field_object, ManyToManyFieldInstance) and value is not None:
            target_pk_attr = field_object.related_model._meta.pk_attr
            if isinstance(target_pk_attr, tuple):
                field_names = tuple(f"{key}__{pk_name}" for pk_name in target_pk_attr)
                return KeyColumns.get_comparison_q(key, field_names, target_pk_attr, Lookup.EXACT, value)
        return Q(**{key: value})

    @staticmethod
    @ModelCache.fact()
    def get_lazy_relation_names(model: type[Model]) -> tuple[frozenset[str], frozenset[str]]:
        """The forward FK/O2O and M2M relations of ``model`` a query loads by default - declared
        ``lazy=RelationLoadStrategy.JOINED`` and ``lazy=RelationLoadStrategy.SELECT``.

        Returns:
            ``(joined relation names, prefetched relation names)``.
        """
        meta = model._meta
        joined: set[str] = set()
        prefetched: set[str] = set()
        for field_name in meta.fk_fields | meta.o2o_fields | meta.m2m_fields:
            lazy = getattr(meta.fields_map[field_name], "lazy", None)
            if lazy == RelationLoadStrategy.JOINED:
                joined.add(field_name)
            elif lazy == RelationLoadStrategy.SELECT:
                prefetched.add(field_name)
        return frozenset(joined), frozenset(prefetched)

    def _apply_lazy_relation_defaults(self) -> None:
        """Adds the relations the model loads by default (``lazy=``) and ``.defer_related()``
        didn't switch off to the ones this query joins or prefetches - on the copy a build makes,
        before it reads them."""
        joined, prefetched = QuerySpec.get_lazy_relation_names(self.model)
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
        joined, prefetched = QuerySpec.get_lazy_relation_names(self.model)
        return bool((joined | prefetched) - self._deferred_related_fields)

    @classmethod
    def copy_spec(cls, source: QuerySpec[Any], target: QuerySpec[Any]) -> None:
        """Hands every setting of a specification to a query built from it - a container the build
        changes as its own copy.

        Args:
            source: The specification.
            target: The query.
        """
        copy_function = QuerySpec.spec_copy_cache
        if copy_function is None:
            # One direct attribute assignment per slot, like QuerySpec._compiled_copy().
            lines = [
                QuerySpec._get_slot_copy_source(name, "source", "target", QuerySpec.MUTABLE_SPEC_SLOTS)
                for name in QuerySpec.SPEC_SLOTS
            ]
            namespace: dict[str, Any] = {}
            exec("def copy_spec(source, target):\n" + "\n".join(lines), namespace)  # noqa: S102 # nosec B102 - class-derived source, no external input
            copy_function = QuerySpec.spec_copy_cache = cast("Callable[[Any, Any], None]", namespace["copy_spec"])
        copy_function(source, target)
        prefetch_queries = source._prefetch_queries
        target._prefetch_queries = (
            {key: list(value) for key, value in prefetch_queries.items()} if prefetch_queries else {}
        )

    def take_rows_of(
        self, source: QuerySpec[Any], *, keeps_ordering: bool = False, keeps_grouping: bool = False
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
        QuerySpec.copy_spec(source, self)
        self._single = False
        self._raise_does_not_exist = False
        self._prefetch_map = {}
        self._prefetch_queries = {}
        self._select_related = set()
        self._annotation_descriptions = None
        self._selection = None
        self._combination = None
        self._direct_get = None
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
            if FORBIDDEN_ANNOTATION_NAME_PATTERN.search(name):
                raise QueryError(
                    f"Annotation name {name!r} can't contain whitespace, quotation marks, brackets, "
                    "semicolons or SQL comments"
                )

    def _raise_object_does_not_exist(self) -> NoReturn:
        """Raises for a query that matched no row: ``DoesNotExist``, or the exception ``.get(...,
        exception=...)`` named - a class is instantiated with no arguments, an instance is raised as
        is.
        """
        if self._does_not_exist_exception is not None:
            override = self._does_not_exist_exception
            raise override if isinstance(override, BaseException) else override()
        raise DoesNotExist(self.model)

    def _empty_single_or_list_result(self) -> Any:
        """The result of a rows query that matched nothing - what ``.none()`` returns without a query.

        Raises:
            DoesNotExist: The query is for a single row that must exist.
        """
        if self._single:
            if self._raise_does_not_exist:
                self._raise_object_does_not_exist()
            return None
        return []

    @property
    def features(self) -> Features:
        """What the connection the query runs on supports.

        Raises:
            QueryError: No connection is chosen yet - the query isn't running.
        """
        if self._features is None:
            self._features = self._get_bound_db().features
        return self._features

    @property
    def dialect(self) -> Dialect:
        """The dialect of the connection the query runs on.

        Raises:
            QueryError: No connection is chosen yet - the query isn't running.
        """
        return self._get_bound_db().dialect

    def _get_analysis_dialect(self) -> Dialect:
        """The dialect expressions are resolved with to analyse the query's shape - the chosen
        connection's, else the neutral SQL dialect before the query runs."""
        db = self._db
        return SQL_DIALECT if db is None else db.dialect

    def _get_bound_db(self) -> DatabaseClient:
        """The connection the query runs on.

        Raises:
            QueryError: No connection is chosen yet - the query isn't running.
        """
        db = self._db
        if db is None:
            raise QueryError(
                f"The {type(self).__name__} on {self.model.__name__} has no connection yet - "
                "the connection, its dialect and features are chosen when the query runs"
            )
        return db

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
            dialect=self._get_analysis_dialect(),
            connection=self._db,
            table=self._effective_basetable(),
            annotations=annotations,
            aggregated_multi_valued_paths=aggregated_multi_valued_paths,
            multi_valued_join_generations=multi_valued_join_generations,
            select_related_extra_conditions=self._select_related_extra_conditions,
            visibility=self._visibility,
            # Building the real query rejects a filter on a window function where SQL can't apply it.
            window_function_filter_allowed=True,
        )

    def _get_row_multiplying_annotation_names(self, *, selected_only: bool) -> list[str]:
        """Names of the annotations reading a to-many relation outside any aggregate - the query
        returns a row per related row for each of them.

        Args:
            selected_only: Leave out the ``.alias()`` annotations, which aren't selected.

        Returns:
            The annotation names, in annotation order.
        """
        annotation_names: list[str] = []
        unused_alias_keys = set() if selected_only else self._get_unused_alias_keys()
        for annotation_name, annotation in self._annotations.items():
            if not isinstance(annotation, Expression) or (selected_only and annotation_name in self._alias_keys):
                continue
            if annotation_name in unused_alias_keys:
                continue
            multi_valued_paths = AggregatedMultiValuedPaths()
            multi_valued_paths.is_recording_row_joins = True
            annotation.get_result(
                ExpressionContext(
                    model=self.model,
                    dialect=self._get_analysis_dialect(),
                    connection=self._db,
                    table=self._effective_basetable(),
                    annotations=self._annotations,
                    annotation_names_in_progress={annotation_name},
                    select_related_extra_conditions=self._select_related_extra_conditions,
                    aggregated_multi_valued_paths=multi_valued_paths,
                    visibility=self._visibility,
                )
            )
            if multi_valued_paths.row_paths:
                annotation_names.append(annotation_name)
        return annotation_names

    def _reads_window_function(self) -> bool:
        """Whether an annotation the query reads is computed by a window function - over every
        row the query matches, so a keyset page condition would change its value.

        Returns:
            True when a window function is read.
        """
        unused_alias_keys = self._get_unused_alias_keys(self._get_selected_field_names())
        probe_context = self._get_probe_expression_context(self._annotations)
        for annotation_name, annotation in self._annotations.items():
            if annotation_name in unused_alias_keys:
                continue
            term = annotation.get_result(probe_context).term if isinstance(annotation, Expression) else annotation
            if isinstance(term, Term) and self._term_reads_window_function(term):
                return True
        return False

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
                isinstance(annotation, Expression) and self._annotation_is_group_by_safe(annotation)
                for annotation in annotations.values()
            )
        ):
            return self.model._meta.ordering
        return orderings

    @staticmethod
    def get_concrete_field_paths(model: type[Model], field_path: str) -> tuple[str, ...]:
        """The field paths a path reads: a trailing ``pk`` reads the primary key field(s), a trailing
        forward relation its key field(s) (``dept`` reads ``dept_id``), a trailing to-many or
        reverse one-to-one relation the related model's primary key field(s).

        Args:
            model: The model the path starts from.
            field_path: A ``field``/``relation__field`` path.

        Returns:
            The paths read - the path itself when it names a field or isn't a model field.
        """
        paths: dict[str, tuple[str, ...]] | None = model._meta.concrete_field_paths
        if paths is None:
            paths = QuerySpec.concrete_field_paths.get_model_bucket(model)
        concrete_field_paths = paths.get(field_path)
        if concrete_field_paths is None:
            concrete_field_paths = paths[field_path] = QuerySpec._read_concrete_field_paths(model, field_path)
        return concrete_field_paths

    @staticmethod
    def _read_concrete_field_paths(model: type[Model], field_path: str) -> tuple[str, ...]:
        """Reads the field paths a path reads - see ``get_concrete_field_paths()``.

        Args:
            model: The model the path starts from.
            field_path: A ``field``/``relation__field`` path.

        Returns:
            The paths read.
        """
        lookup_path = LookupPath.parse(model, field_path)
        if len(lookup_path.rest) != 1:
            return (field_path,)
        last_name = lookup_path.rest[0]
        meta = lookup_path.model._meta
        path_prefix = lookup_path.prefix
        if last_name == "pk" and last_name not in meta.fields_map:
            return tuple(f"{path_prefix}{pk_attr_name}" for pk_attr_name in meta.pk_attr_names)
        if last_name not in meta.fetch_fields:
            return (field_path,)
        relation = cast("RelationalField[Model]", meta.fields_map[last_name])
        if last_name in meta.fk_fields or last_name in meta.o2o_fields:
            return tuple(f"{path_prefix}{source_field_name}" for source_field_name in relation.source_fields)
        related_meta = relation.related_model._meta
        if not related_meta.has_primary_key:
            # A related model without a primary key is read by its key column to this row - never
            # NULL in a joined row, like a primary key.
            backward_relation = cast("BackwardFKRelation[Model]", relation)
            source_field_names = [
                related_meta.fields_db_projection_reverse.get(column, column)
                for column in backward_relation.relation_source_fields
            ]
            return tuple(f"{path_prefix}{last_name}__{source_field_name}" for source_field_name in source_field_names)
        return tuple(f"{path_prefix}{last_name}__{pk_attr_name}" for pk_attr_name in related_meta.pk_attr_names)

    @classmethod
    def get_concrete_field_path(cls, model: type[Model], field_path: str) -> str:
        """The one field path a path reads - see ``get_concrete_field_paths()``.

        Args:
            model: The model the path starts from.
            field_path: A ``field``/``relation__field`` path.

        Returns:
            The path read.

        Raises:
            QueryError: The path reads a composite primary key or a composite foreign key -
                there's no single column.
        """
        concrete_field_paths = cls.get_concrete_field_paths(model, field_path)
        if len(concrete_field_paths) != 1:
            component_names = ", ".join(repr(path) for path in concrete_field_paths)
            raise QueryError(
                f"'{field_path}' reads a composite primary key (or a foreign key to one) - there's no single "
                f"column to read. Name its components instead: {component_names}."
            )
        return concrete_field_paths[0]

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
    def _check_streamable(db: DatabaseClient | None) -> TransactionClient:
        """Checks that ``stream()`` can run on ``db``: a server-side cursor / portal, inside a
        transaction - only one transaction gives it a consistent snapshot for the whole iteration.

        Args:
            db: The connection the query runs on.

        Returns:
            The connection - the transaction's client.

        Raises:
            UnSupportedError: The database has no server-side streaming (SQLite).
            QueryError: No transaction is open.
        """
        db = cast("DatabaseClient", db)
        if not db.features.supports_streaming:
            raise UnSupportedError(
                f"stream() is not supported on the {db.dialect.name} backend - it "
                "has no useful server-side row streaming to offer; use iterator() instead"
            )
        if not isinstance(db, TransactionClient):
            raise QueryError(
                "stream() requires an active Transactions.atomic() block - its server-side "
                "cursor/portal only gets a consistent snapshot for the whole iteration when scoped "
                "to one transaction"
            )
        return db

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
        ``OuterRef`` would otherwise bind to this query's own row.
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
        tracker = self.AnnotationAccessTracker(self._annotations)
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

    @staticmethod
    def _term_reads_window_function(term: Term) -> bool:
        """Whether a term reads a window function of its own query.

        Args:
            term: The term.

        Returns:
            True when a window function call appears outside any subquery.
        """
        boundary_node_ids: set[int] = set()
        nodes: Iterator[Any] = term.nodes_()
        for node in nodes:
            if id(node) in boundary_node_ids:
                continue
            if node.is_analytic:
                return True
            if node.is_subquery or isinstance(node, Selectable):
                boundary_node_ids.update(map(id, node.nodes_()))
        return False

    @staticmethod
    def _annotation_is_group_by_safe(annotation: Term | Expression) -> bool:
        """Whether ``annotation`` never makes the query group implicitly - ``Meta.ordering`` is then as
        safe as for an unannotated query. Only the shapes known not to aggregate are recognized.
        """
        if isinstance(annotation, (Value, F, Exists)):
            return True
        if isinstance(annotation, CombinedExpression):
            return QuerySpec._annotation_is_group_by_safe(annotation.left) and QuerySpec._annotation_is_group_by_safe(
                annotation.right
            )
        return False

    # Declared for the type checker: _apply_db() assigns None.
    _features: Features | None

    # Declared here for the same reason as _db/_features above - get_connection() below (this
    # class, not a subclass) reads it directly.
    _router_fallback_db: DatabaseClient | None

    _instance_connection_name: str | None

    _visibility: RowVisibility

    model: type[TModel]

    #: Whether the query takes the QuerySet methods dialects registered (QuerySetExtensions).
    accepts_extension_methods: ClassVar[bool] = False

    #: Whether the query binds the values of an annotation a GROUP BY, ORDER BY or DISTINCT ON
    #: resolves again into its full expression (_get_expression_term_value_refs()) - a query of
    #: another type ordering by such an annotation keeps no plan.
    binds_expression_term_values: ClassVar[bool] = False

    #: The description of a query without keyset boundaries (_get_cursor_plan_description()).
    NO_CURSOR_PLAN_DESCRIPTION: ClassVar[PlanDescription] = PlanDescription(((), ()), [])

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any:
            # A QuerySet method a dialect registered (QuerySetExtensions).
            if self.accepts_extension_methods and name in QuerySetExtensions.registered:
                return QuerySetExtensions.get_method(cast("QuerySet[Any]", self), name)
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    # The slot names of the class, cached per subclass.
    clone_slots_cache: ClassVar[tuple[str, ...] | None] = None

    # The compiled per-subclass copy function _compiled_copy() builds - see its own docstring.
    compiled_copy_cache: ClassVar[Callable[[Any, Any], None] | None] = None

    # The slots holding a mutable container, each with the source of an empty container of its type
    # - a clone gets its own shallow copy.
    mutable_clone_slots: ClassVar[dict[str, str]] = {}

    @classmethod
    def _clone_slots(cls) -> tuple[str, ...]:
        """Every ``__slots__`` name across the class's MRO, cached per subclass - what the compiled
        copy function is generated from.
        """
        cached = cls.__dict__.get("clone_slots_cache")
        if cached is None:
            cached = tuple({slot for klass in cls.__mro__ for slot in klass.__dict__.get("__slots__", ())})
            cls.clone_slots_cache = cached
        return cached

    @classmethod
    def _compiled_copy(cls) -> Callable[[Any, Any], None]:
        """Compiles, once per subclass, ``def _copy(self, newone): newone.a = self.a; ...`` - one
        literal attribute assignment per slot, which the interpreter specializes (unlike
        ``getattr``/``setattr`` by name). A slot in ``mutable_clone_slots`` is assigned its
        ``.copy()``.
        """
        cached = cls.__dict__.get("compiled_copy_cache")
        if cached is not None:
            return cached
        mutable_slots = cls.mutable_clone_slots
        lines = [QuerySpec._get_slot_copy_source(name, "self", "newone", mutable_slots) for name in cls._clone_slots()]
        body = "\n".join(lines) or "    pass"
        namespace: dict[str, Any] = {}
        exec(f"def _copy(self, newone):\n{body}", namespace)  # noqa: S102 # nosec B102 - class-derived source, no external input
        compiled = cast("Callable[[Any, Any], None]", namespace["_copy"])
        cls.compiled_copy_cache = compiled
        return compiled

    @staticmethod
    def _get_slot_copy_source(name: str, source: str, target: str, mutable_slots: dict[str, str]) -> str:
        """The line of a compiled copy function copying one slot: a mutable container gets its own
        copy - a new empty one when it is empty, without a call.

        Args:
            name: The slot.
            source: The name of the object copied from.
            target: The name of the object copied to.
            mutable_slots: The slots holding a mutable container, each with the source of an empty
                container of its type.

        Returns:
            The line.
        """
        empty_source = mutable_slots.get(name)
        if empty_source is None:
            return f"    {target}.{name} = {source}.{name}"
        return f"    {target}.{name} = {source}.{name}.copy() if {source}.{name} else {empty_source}"

    def __copy__(self) -> QuerySpec[TModel]:
        cls = type(self)
        newone = cls.__new__(cls)
        (cls.__dict__.get("compiled_copy_cache") or cls._compiled_copy())(self, newone)
        # A subclass without __slots__ keeps its own attributes in a __dict__ - asked of the class:
        # hasattr() on a slotted instance would raise through __getattr__().
        if cls.__dictoffset__:
            newone.__dict__.update(self.__dict__)
        return newone

    def get_connection(self, for_write: bool = False) -> DatabaseClient:
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
            ConfigurationError: No connection is pinned and no Hare context is active.
            QueryError: The model has no default connection.
        """
        if self._db:
            return self._db
        # Imported here: the modules import each other.
        from hare.core.context import HareContext

        # HareContext.require_current() - its calls left for the error.
        ctx = HareContext.current_context.get() or HareContext.global_context or HareContext.require_current()
        router = ctx.router
        # The router is asked under the tenant this query's WHERE was built for, not the one active
        # when the query is awaited - a router routing by tenant would otherwise pick another
        # tenant's database.
        tenant_snapshot = self._visibility.tenant
        if (
            tenant_snapshot is AMBIENT_TENANT_NOT_OVERRIDDEN
            or tenant_snapshot is None
            or tenant_snapshot is Tenancy.current.get()
        ):
            db = router.db_for_write(self.model) if for_write else router.db_for_read(self.model)
        else:
            tenant_token = Tenancy.current.set(tenant_snapshot)
            try:
                db = router.db_for_write(self.model) if for_write else router.db_for_read(self.model)
            finally:
                Tenancy.current.reset(tenant_token)
        if db is not None:
            return db
        if self._instance_connection_name is not None:
            return ctx.connections.get(self._instance_connection_name)
        if self._router_fallback_db is not None:
            return self._router_fallback_db
        # The model's default connection, read from the context already in hand - this is the
        # hottest path of the ORM.
        meta = self.model._meta
        default_connection = meta.default_connection
        if default_connection is None:
            raise ConfigurationError(f"default_connection for the model {meta._model} cannot be None")
        return ctx.connections.get(default_connection)

    def _apply_db(self, db: DatabaseClient | None) -> None:
        """Binds this query to the connection it runs on.

        Args:
            db: The database connection to use for this query.
        """
        # None leaves the connection to be chosen when the query runs.
        self._db = cast("DatabaseClient", db)
        self._features = None

    def _get_base_query(self) -> QueryBuilder:
        """A fresh ``SELECT ... FROM`` the model's table, built for the connection the query runs on.

        Returns:
            The query builder.
        """
        meta = self.model._meta
        db = self._db if self._db is not None else meta.db
        if db.query_class is meta.db.query_class:
            return copy(cast("QueryBuilder", meta.basequery))
        return db.query_class.from_(meta.basetable)

    def _get_base_query_all_fields(self) -> QueryBuilder:
        """``_get_base_query()`` selecting every column of the model.

        Returns:
            The query builder.
        """
        meta = self.model._meta
        db = self._db if self._db is not None else meta.db
        if db.query_class is meta.db.query_class:
            return copy(cast("QueryBuilder", meta.basequery_all_fields))
        return db.query_class.from_(meta.basetable).select(*meta.db_fields)

    def _get_execution_query(self, for_write: bool = False) -> Self:
        """Returns the query to run now: itself when its connection is pinned, otherwise a copy
        bound to the connection chosen for this one execution, so a queryset evaluated again later
        (after a transaction ends, inside another one, from another task) chooses afresh.

        Args:
            for_write: Whether the connection is chosen for a write.

        Returns:
            The query bound to a connection.
        """
        if cast("DatabaseClient | None", self._db) is not None:
            return self
        execution_query = copy(self)
        execution_query._apply_db(execution_query.get_connection(for_write))
        return execution_query

    def get_pinned_connection_name(self) -> str | None:
        """The name of the connection the query is pinned to with ``.using()`` - None for a query
        that picks its connection when it runs."""
        db = self._db
        return db.connection_name if db is not None else None

    def get_bound_to(self, outer_db: DatabaseClient | None, outer_model: type[Model], embedded_as: str) -> Self:
        """This query as it is built into another one - bound to the connection the outer query runs
        on, since both compile to one SQL text. Without ``.using()`` it takes the outer connection;
        pinned to the same one it is kept as is.

        Args:
            outer_db: The outer query's connection - None for a query compiled outside execution.
            outer_model: The outer query's model, for the message.
            embedded_as: What this query is in the outer one, for the message.

        Returns:
            A copy bound to the outer query's connection.

        Raises:
            QueryError: This query is pinned to another connection than the outer query's.
        """
        if outer_db is None:
            return self._get_execution_query()
        pinned_db = self._db
        if pinned_db is not None and pinned_db.connection_name != outer_db.connection_name:
            raise QueryError(
                f"{self.model.__name__} query used as {embedded_as} is pinned to a different database "
                f"connection ({pinned_db.connection_name!r}) than the outer {outer_model.__name__} query runs "
                f"on ({outer_db.connection_name!r}) - both compile to one SQL text, run on the outer query's "
                "connection. Remove the inner queryset's .using() override, or query each connection "
                "separately instead."
            )
        bound_query = copy(self)
        bound_query._apply_db(outer_db)
        return bound_query

    def _get_compiler(self) -> QuerySpec[Any]:
        """The query building this query's SQL - the query itself, unless it only describes the
        rows another query builds.

        Returns:
            The query.
        """
        return self

    def _make_query(self) -> None:
        """Builds the query for the connection it runs on."""
        raise NotImplementedError()  # pragma: nocoverage

    def _get_statements(self, params_inline: bool) -> list[tuple[str, list[Any]]]:
        """The statements the query runs, built for the connection it runs on.

        Args:
            params_inline: Whether values are rendered into the SQL instead of bound.

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
        dialect = self._db.dialect
        return [(dialect.get_explain_sql(sql, output_format, options), values) for sql, values in statements]

    def sql(
        self,
        params_inline: bool = False,
        *,
        explain: bool = False,
        output_format: str | None = None,
        **explain_options: bool,
    ) -> str:
        """The SQL the query runs, in the dialect of the connection it runs on.

        Args:
            params_inline: Whether values are rendered into the SQL instead of placeholders.
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
        statements = execution_query._get_statements(params_inline)
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
        db = execution_query._db
        statements = execution_query._get_explain_statements(
            execution_query._get_statements(params_inline=False), output_format, options
        )
        rows: list[Any] = []
        for sql, values in statements:
            rows.extend((await db.execute(sql, values))[1])
        return rows


# The query classes are defined above, and the expressions imported - a value of any of them is
# written into a statement or a subquery, never bound as a parameter.
StatementPlan.unbindable_value_classes = (Term, Expression, QuerySpec)
