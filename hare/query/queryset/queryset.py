from __future__ import annotations

from collections.abc import AsyncIterator, Generator, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import tzinfo as TzInfo
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, Self, cast, overload

from typing_extensions import TypeVar

from hare.core.connections.connections import Connections
from hare.dialects.base.clauses.enums import RowLockStrength
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import (
    DoesNotExist,
    FieldError,
    QueryError,
)
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.models.class_building.blind_indexes import BlindIndexes
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.models.instances.instance_connections import InstanceConnections
from hare.models.tenancy.tenancy import Tenancy
from hare.query.constants import AGGREGATE_OVER_DISTINCT_ON_MESSAGE, GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK
from hare.query.enums import GetException, RowShape, TableSampleMethod
from hare.query.expressions import Exists, Expression, F, Ordering, Q
from hare.query.expressions.joins.named_join import NamedJoin
from hare.query.expressions.subqueries.recursive_rows import RecursiveRows
from hare.query.filters.resolution.filter_values import FilterValues
from hare.query.functions.datetime.trunc import Trunc
from hare.query.functions.math.random import Random
from hare.query.generic_foreign_keys.generic_foreign_key_filters import GenericForeignKeyFilters
from hare.query.generic_foreign_keys.generic_foreign_key_paths import GenericForeignKeyPaths
from hare.query.generic_foreign_keys.generic_foreign_key_types import GenericForeignKeyTypes
from hare.query.grouping.grouping_set import GroupingSet
from hare.query.lookup_info.lookup_info import LookupInfo
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.query.lookup_info.lookup_path import LookupPath
from hare.query.lookup_info.ordering_info import OrderingInfo
from hare.query.plans.call_signatures.call_signature_runs import CallSignatureRuns
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.query.queryset.arguments.annotation_arguments import AnnotationArguments
from hare.query.queryset.arguments.filter_arguments import FilterArguments
from hare.query.queryset.arguments.ordering_arguments import OrderingArguments
from hare.query.queryset.arguments.values_arguments import ValuesArguments
from hare.query.queryset.combination.queryset_combination import QuerySetCombination
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.queryset.constants import (
    AGGREGATE_OVER_GROUPING_SETS_MESSAGE,
    DATES_TRUNC_TYPES,
    DATETIMES_TRUNC_TYPES,
    LATEST_WITHOUT_FIELDS_MESSAGE,
    RANDOM_ORDERING,
    RANDOM_ORDERING_ALIAS,
)
from hare.query.queryset.date_lists import DateLists
from hare.query.queryset.declarations import NoAnnotations
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions
from hare.query.queryset.options.query_options import QueryOptions
from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup
from hare.query.queryset.pending_calls.pending_filter_calls import PendingFilterCalls
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.queryset.schema_object_calls import SchemaObjectCalls
from hare.query.queryset.selection.statement_selection import StatementSelection
from hare.query.queryset.selection.values_selection import ValuesSelection
from hare.query.queryset.single_rows.get_exceptions import GetExceptions
from hare.query.queryset.single_rows.query_set_single import QuerySetSingle
from hare.query.queryset.single_rows.single_row import SingleRow
from hare.query.queryset.table_sample import TableSample
from hare.query.relation_loading.prefetching.prefetch import Prefetch
from hare.query.relation_loading.select import Select
from hare.query.rewrites.query_rewrites import QueryRewrites
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.select.combined.combined_derived_queries import CombinedDerivedQueries
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from hare.query.statements.select.rows_query import RowsQuery
from hare.query.statements.select.values.values_grouping import ValuesGrouping
from hare.query.statements.write.create_or_update import CreateOrUpdate
from hare.sql import Order
from hare.sql.builder.tables.selectable import Selectable
from hare.sql.enums import SetOperation, TruncType
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.raw_sql_term import RawSQLTerm
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.queryset.single_rows.get_exception_argument import GetExceptionArgument
    from hare.query.statements.select.values_query import ValuesQuery

TModel = TypeVar("TModel", bound="Model")
#: What a row of a queryset is - the model instance, or what ``.values()``/``.values_list()`` select.
TRow = TypeVar("TRow", default=TModel)
#: The names and types of the queryset's ``.annotate()``/``.alias()`` expressions, as a ``TypedDict`` -
#: only the type checker reads it (the ``hare.contrib.mypy`` plugin fills it in). Covariant: a
#: queryset with annotations is still a ``QuerySet[Model]``.
TAnnotations = TypeVar("TAnnotations", bound=NoAnnotations, covariant=True, default=NoAnnotations)
TDerivedQuery = TypeVar("TDerivedQuery", bound="AwaitableQuery[Any]")
TMadeQuerySet = TypeVar("TMadeQuerySet", bound="QuerySet[Any, Any]")


class QuerySet(QuerySpecification[TModel], Generic[TModel, TRow, TAnnotations]):
    """A query for the rows of a model - model instances, the values ``.values()``/``.values_list()``
    select, or the rows a set operation combines. Every chained method returns a changed copy;
    awaiting, iterating or streaming it builds its SQL and runs it.
    """

    accepts_extension_methods: ClassVar[bool] = True

    __slots__ = (
        # The calls this queryset was made with from its manager's queryset, when it was made by
        # simple calls alone (filter()/exclude() of plain values, order_by() of names, slices,
        # first()/last(), values()/values_list()/only()/select_related() of names) - None
        # otherwise. Its queries run on the plan kept under the key of the calls, built not even in
        # part. Any other change - a clone, an in-place change - leaves None.
        "_call_signature",
        # The values of the signature's filter calls, in call order.
        "_call_values",
        # The object this queryset, made again for each description or build of a query it is
        # part of, stands for - unset otherwise (PlanOrigins).
        "_plan_origin",
    )

    # The dict/list/set slots a clone gets its own shallow copy of. `_prefetch_queries` is a dict of
    # lists - _clone() copies it separately.
    mutable_clone_slots: ClassVar[dict[str, str]] = QuerySpecification.MUTABLE_SPECIFICATION_SLOTS

    def __init__(self, model: type[TModel]) -> None:
        model._meta.check_not_swapped()
        super().__init__(model)
        self._call_signature: tuple[Any, ...] | None = None
        self._call_values: tuple[Any, ...] = ()

    def __copy__(self) -> Self:
        queryset = cast("Self", super().__copy__())
        # A copy is changed by whoever made it - its calls aren't known.
        queryset._call_signature = None
        return queryset

    def _continue_call_signature(
        self, queryset: TMadeQuerySet, call: tuple[Any, ...] | None, values: tuple[Any, ...] = ()
    ) -> TMadeQuerySet:
        """Gives a queryset made from this one by a simple call this one's call signature with the
        call appended.

        Args:
            queryset: The queryset made.
            call: The call - its name and the arguments shaping the SQL; None for a call changing
                nothing.
            values: The filter values the call binds.

        Returns:
            ``queryset``.
        """
        call_signature = self._call_signature
        if call_signature is not None:
            queryset._call_signature = call_signature if call is None else (*call_signature, call)
            queryset._call_values = (*self._call_values, *values) if values else self._call_values
        return queryset

    def _hand_call_signature(self, query: AwaitableQuery[Any]) -> None:
        """Hands this queryset's call signature to the query made to run it.

        Args:
            query: The query.
        """
        if self._call_signature is not None:
            query._call_signature = self._call_signature
            query._call_values = self._call_values

    @CallsBeforeSetup.recorded
    def all_tenants(self) -> Self:
        """Returns a clone spanning every tenant - without the ``Meta.tenant_field`` filter, for this
        model and for the tenant-scoped models the query reaches through relations.
        """
        QuerySetCombination.raise_if_combined(self, "all_tenants")
        queryset = self._clone()
        queryset._visibility = replace(queryset._visibility, all_tenants=True)
        return queryset

    @CallsBeforeSetup.recorded
    def include_deleted(self) -> Self:
        """Returns a clone including soft-deleted rows. After ``.only_deleted()`` it replaces it.

        Raises:
            QueryError: The model has no ``Meta.soft_delete_field``.
        """
        QuerySetCombination.raise_if_combined(self, "include_deleted")
        return self._with_deleted_rows(only_deleted=False)

    @CallsBeforeSetup.recorded
    def only_deleted(self) -> Self:
        """Returns a clone with only soft-deleted rows; relations reached from it see deleted rows too.
        The ``Meta.tenant_field`` filter still applies. After ``.include_deleted()`` it replaces it.

        Raises:
            QueryError: The model has no ``Meta.soft_delete_field``.
        """
        QuerySetCombination.raise_if_combined(self, "only_deleted")
        return self._with_deleted_rows(only_deleted=True)

    def _with_deleted_rows(self, *, only_deleted: bool) -> Self:
        """A clone seeing soft-deleted rows - them alone, or together with the live ones.

        Args:
            only_deleted: See only the soft-deleted rows of the model itself.

        Raises:
            QueryError: If the model has no ``Meta.soft_delete_field`` configured.
        """
        QuerySetCombination.raise_if_combined(self, "only_deleted" if only_deleted else "include_deleted")
        if not self.model._meta.soft_delete_field:
            raise QueryError(f"{self.model.__name__} has no Meta.soft_delete_field configured")
        queryset = self._clone()
        queryset._visibility = replace(queryset._visibility, include_deleted=True, only_deleted=only_deleted)
        return queryset

    def _clone(self) -> Self:
        queryset = self._clone_keeping_calls()
        queryset._call_signature = None
        return queryset

    def _clone_keeping_calls(self) -> Self:
        """A clone keeping this queryset's call signature - for a simple call to extend it.

        Returns:
            The clone.
        """
        # _prefetch_queries is a dict of lists - each list is copied, so clones don't share them.
        queryset: Self = QuerySpecification.__copy__(self)  # type: ignore[assignment]
        prefetch_queries = self._prefetch_queries
        queryset._prefetch_queries = (
            {key: list(value) for key, value in prefetch_queries.items()} if prefetch_queries else {}
        )
        return queryset

    def _append_filters(self, negate: bool, args: tuple[Q | Exists, ...], kwargs: dict[str, Any]) -> None:
        """Appends the Q objects built from ``args``/``kwargs`` onto ``self._q_objects``, without
        cloning. Everything one call appends shares one filter-call generation, so its conditions
        over a to-many relation share a JOIN; a later call gets a new one.
        """
        self._call_signature = None
        self._filter_call_counter += 1
        generation = self._filter_call_counter
        if not all(isinstance(arg, (Q, Exists)) for arg in args):
            raise TypeError("expected Q objects or Exists(...) conditions as args")
        conditions = [(arg if isinstance(arg, Q) else Q(arg))._with_filter_call_generation(generation) for arg in args]
        conditions.extend(PendingFilterCalls.get_filter_kwarg_conditions(self, kwargs, generation))
        PendingFilterCalls.add_filter_conditions(self, negate, conditions, generation)

    def _filter_or_exclude(self, negate: bool, args: tuple[Q | Exists, ...], kwargs: dict[str, Any]) -> Self:
        FilterArguments.raise_if_slice_taken(self, "Cannot filter a query once a slice has been taken.")
        if GenericForeignKeyFieldInstance.declared_names:
            args, kwargs = GenericForeignKeyFilters.rewrite_arguments(self.model, args, kwargs)
        FilterArguments.check_filter_keys(self, args, kwargs, "exclude" if negate else "filter")
        # An iterable __in value is read once - the filter and the call signature share the list.
        kwargs = {key: FilterValues.get_list_lookup_value(key, value) for key, value in kwargs.items()}
        if (
            self._call_signature is not None
            and (not args or FilterArguments.takes_pending_conditions(self, args))
            and FilterArguments.takes_pending_filters(self, kwargs)
        ):
            for value in kwargs.values():
                # A SQL term built by hand describes itself by its rendered text alone.
                if isinstance(value, Term) and not isinstance(value, Plannable):
                    break
            else:
                # Built only when a query of the queryset doesn't run on the plan of its calls.
                queryset = self._clone_keeping_calls()
                queryset._filter_call_counter += 1
                generation = queryset._filter_call_counter
                conditions = tuple(
                    [(arg if isinstance(arg, Q) else Q(arg))._with_filter_call_generation(generation) for arg in args]
                )
                queryset._pending_filter_calls = (
                    *self._pending_filter_calls,
                    (negate, generation, conditions, kwargs),
                )
                if not conditions:
                    return self._continue_call_signature(
                        queryset, ("exclude" if negate else "filter", tuple(kwargs)), tuple(kwargs.values())
                    )
                # The conditions come first - each is described by its tree in the key of the calls.
                return self._continue_call_signature(
                    queryset,
                    ("exclude_conditions" if negate else "filter_conditions", tuple(kwargs), len(conditions)),
                    (*conditions, *kwargs.values()),
                )
        queryset = self._clone()
        queryset._append_filters(negate, args, kwargs)
        return queryset

    def get_lookup_info(self, key: str) -> LookupInfo:
        """Describes a ``.filter()`` key of this queryset - like ``Model._meta.get_lookup_info()``,
        and a key may start with one of the queryset's annotations, described by the
        annotation's output field.

        Args:
            key: The filter key.

        Returns:
            The description.

        Raises:
            FieldError: The key names no field, relation or annotation, or a lookup its field
                doesn't have.
            QueryError: The key is a lookup other than equality, membership or ``isnull`` on a
                relation to a composite key.
            QueryError: The model isn't bound yet (``Model._meta.is_bound``).
        """
        self.model._meta._check_bound()
        if key.partition("__")[0] not in self._annotations:
            return self.model._meta.get_lookup_info(key)
        return AnnotationArguments.get_annotation_description(
            self,
            "lookup_info",
            key,
            lambda: LookupInfoBuilder.get_lookup_info(
                self.model,
                key,
                annotations=self._annotations,
                annotation_fields=AnnotationArguments.get_annotation_output_fields(self),
            ),
        )

    def get_lookups(self, path: str, dialect: Dialect | None = None) -> dict[str, LookupInfo]:
        """Describes every lookup of a field path or annotation of this queryset that a dialect
        runs - see ``Model._meta.get_lookups()``.

        Args:
            path: A field, relation or annotation, after any relations.
            dialect: The dialect - by default the one of the connection this queryset runs on.

        Returns:
            Each lookup's suffix after ``path`` to its description.

        Raises:
            FieldError: The path names no field, relation or annotation.
            QueryError: The model isn't bound yet (``Model._meta.is_bound``).
        """
        self.model._meta._check_bound()
        if dialect is None:
            dialect = self.get_connection().dialect
        if path.partition("__")[0] not in self._annotations:
            return self.model._meta.get_lookups(path, dialect)
        lookups = AnnotationArguments.get_annotation_description(
            self,
            f"lookups:{dialect.name}",
            path,
            lambda: LookupInfoBuilder.get_lookups(
                self.model,
                path,
                dialect,
                annotations=self._annotations,
                annotation_fields=AnnotationArguments.get_annotation_output_fields(self),
            ),
        )
        return dict(lookups)

    def get_ordering_info(self, name: str) -> OrderingInfo:
        """Describes an ``.order_by()`` name of this queryset - like
        ``Model._meta.get_ordering_info()``, and a name may be one of the queryset's annotations.

        Args:
            name: The ordering name, optionally with a leading ``-``.

        Returns:
            The description.

        Raises:
            FieldError: The name names no field, relation or annotation.
            QueryError: The model isn't bound yet (``Model._meta.is_bound``).
        """
        self.model._meta._check_bound()
        if name.removeprefix("-").partition("__")[0] not in self._annotations:
            return self.model._meta.get_ordering_info(name)
        return AnnotationArguments.get_annotation_description(
            self,
            "ordering_info",
            name,
            lambda: LookupInfoBuilder.get_ordering_info(
                self.model,
                name,
                annotations=self._annotations,
                annotation_fields=AnnotationArguments.get_annotation_output_fields(self),
            ),
        )

    @CallsBeforeSetup.recorded
    def filter(self, *args: Q | Exists, **kwargs: Any) -> Self:
        """
        Filters QuerySet by given kwargs. You can filter by related objects like this:

        Example:
            ::

                Team.objects.filter(events__tournament__name="Test")

        You can also pass Q objects (or ``Exists(...)`` conditions) to filters as args.

        Raises:
            QueryError: The queryset is sliced.
        """
        QuerySetCombination.raise_if_combined(self, "filter")
        return self._filter_or_exclude(False, args, kwargs)

    @CallsBeforeSetup.recorded
    def exclude(self, *args: Q | Exists, **kwargs: Any) -> Self:
        """
        Same as .filter(), but excludes the rows matching all given conditions together -
        ``exclude(a=1, b=2)`` is ``NOT (a = 1 AND b = 2)``, like ``exclude(Q(a=1, b=2))``.
        """
        QuerySetCombination.raise_if_combined(self, "exclude")
        return self._filter_or_exclude(True, args, kwargs)

    def _get_field_may_be_null(self, field_name: str) -> bool:
        """Whether ordering by ``field_name`` can meet NULLs: a nullable column, or anything that is
        not a plain column of this model (a related-model path - its LEFT JOIN yields NULLs - or an
        annotation)."""
        field_object = self.model._meta.fields_map.get(field_name)
        return field_object is None or field_object.null

    @property
    def orderings(self) -> tuple[tuple[str, Order], ...]:
        """The ordering ``order_by()`` gave the queryset, one ``(path, Order)`` per column: ``pk``
        stands for every field of the primary key, a forward relation for its key column(s), an
        ``Ordering`` for its field, direction and NULL placement.

        Empty when ``order_by()`` wasn't called (``Meta.ordering`` is never included) and after
        ``order_by()`` with no arguments. ``before_cursor()`` doesn't change it.
        """
        orderings = tuple(self._orderings)
        if self._reverse_result_order:
            return tuple((path, order.get_reversed()) for path, order in orderings)
        return orderings

    @CallsBeforeSetup.recorded
    def order_by(self, *orderings: str | Ordering) -> Self:
        """
        Accept args to filter by in format like this:

        Example:
            ::

                .order_by('name', '-tournament__name')
                .order_by(F('score').desc(nulls_last=True), 'name')
                .order_by('-created__year', 'data__owner')

        Supports ordering by related models too, and by a path inside a field's value as in
        ``.values()`` - a JSON key, an array item, a part of a date, time or datetime.
        A '-' before the name will result in descending sort order, default is ascending.
        ``order_by()`` with no arguments removes every ordering, ``Meta.ordering`` included - the
        rows come in whatever order the database returns them.
        A plain string leaves the position of NULLs to the dialect (SQLite: NULLs are the smallest
        values, so first for ASC and last for DESC; PostgreSQL: the opposite). To fix it on every
        dialect, pass ``F('field').asc()``/``F('field').desc()`` with ``nulls_first=True`` or
        ``nulls_last=True``.

        Raises:
            FieldError: If unknown field has been provided.
            QueryError: The queryset is sliced (``[a:b]``, ``.limit()``, ``.offset()``) - order it
                before slicing.
            ValueError: If ``.after_cursor()`` was already called - its cursor values are
                positionally bound to the ordering that was active at that point (see its own
                docstring); replacing the ordering afterward would either silently pair values
                against the wrong field/direction, or crash with a confusing bare ``ValueError``
                from ``zip(..., strict=True)`` deep inside query building if the lengths also
                happen to differ. Call ``.order_by()`` before ``.after_cursor()``, not after.
        """
        if self._combination is not None:
            return cast(
                "Self",
                CombinedDerivedQueries.get_ordered_queryset(QuerySetCombination.get_combined_query(self), orderings),
            )
        FilterArguments.raise_if_slice_taken(self, "Cannot reorder a query once a slice has been taken.")
        if GenericForeignKeyFieldInstance.declared_names and (
            type_aliases := GenericForeignKeyTypes.get_pending_type_aliases(self, orderings)
        ):
            return self.alias(**type_aliases).order_by(*orderings)
        # A loop, not any(): every order_by() runs it, and a generator is a call more.
        is_plain = True
        for ordering in orderings:
            if type(ordering) is not str:
                is_plain = False
            elif ordering == RANDOM_ORDERING:
                return self.alias(**{RANDOM_ORDERING_ALIAS: Random()}).order_by(
                    *(
                        RANDOM_ORDERING_ALIAS if type(ordering) is str and ordering == RANDOM_ORDERING else ordering
                        for ordering in orderings
                    )
                )
        OrderingArguments.forbid_reordering_after_cursor(self, "order_by")
        queryset = self._with_orderings(*orderings)
        queryset._default_ordering_disabled = not orderings
        if is_plain:
            return self._continue_call_signature(queryset, ("order_by", orderings))
        return queryset

    def _with_orderings(self, *orderings: str | Ordering) -> Self:
        """A clone ordered by the given fields, even when sliced - for a single-row query that
        orders the slice itself.

        Args:
            orderings: The ordering fields.

        Returns:
            The clone.
        """
        queryset = self._clone()
        if not self._annotations:
            plain_orderings = OrderingArguments.get_plain_orderings(self, orderings)
            if plain_orderings is not None:
                queryset._orderings = list(plain_orderings)
                return queryset
        # A path into a JSON field or annotation orders by an alias of its own, never selected.
        path_annotations = AnnotationArguments.get_path_annotations(
            self, (self._get_ordering_string(ordering)[0] for ordering in orderings)
        )
        if path_annotations:
            AnnotationArguments.detach_annotation_descriptions(queryset, path_annotations)
            queryset._annotations = {**queryset._annotations, **path_annotations}
            queryset._alias_keys = queryset._alias_keys | set(path_annotations)
        queryset._orderings = OrderingArguments.parse_orderings(queryset, orderings)
        return queryset

    @CallsBeforeSetup.recorded
    def after_cursor(self, *values: Any) -> Self:
        """Keyset pagination: filters to the rows strictly after the given values in the current
        ``.order_by(...)``. With ``.before_cursor()`` it makes a window; calling it again replaces
        the lower boundary.

        Args:
            values: The ordering values of the last row of the previous page, one per ordering
                field.

        Raises:
            ValueError: ``.order_by()`` wasn't called, or the number of values doesn't match the
                ordering fields.
            FieldError: An ordering field is an annotation.
        """
        QuerySetCombination.raise_if_combined(self, "after_cursor")
        OrderingArguments.check_cursor_values(self, "after_cursor", values)
        queryset = SingleRow.with_distinct_on_rows_as_subquery(self)
        if queryset._reverse_result_order:
            # .before_cursor() already reversed _orderings - "after" in the caller's ordering is
            # "before" in the reversed one.
            queryset._before_cursor_values = values
        else:
            queryset._cursor_values = values
        return queryset

    @CallsBeforeSetup.recorded
    def before_cursor(self, *values: Any) -> Self:
        """Keyset pagination backwards: filters to the rows strictly before the given values in the
        current ``.order_by(...)``. The query runs in the reversed ordering, so ``.limit(n)`` takes
        the ``n`` rows closest to the cursor; the fetched rows are returned in the original order.
        ``.first()``/``.get()`` pick the row right before the cursor. With ``.after_cursor()`` it
        makes a window; calling it again replaces the upper boundary.

        Args:
            values: The ordering values of the first row of the current page, one per ordering
                field.

        Raises:
            ValueError: ``.order_by()`` wasn't called, or the number of values doesn't match the
                ordering fields.
            FieldError: An ordering field is an annotation.
        """
        QuerySetCombination.raise_if_combined(self, "before_cursor")
        OrderingArguments.check_cursor_values(self, "before_cursor", values)
        queryset = SingleRow.with_distinct_on_rows_as_subquery(self)
        if not queryset._reverse_result_order:
            queryset._orderings = [(field_name, order.get_reversed()) for field_name, order in self._orderings]
            # A lower boundary from an earlier .after_cursor() becomes the upper one in the
            # reversed ordering.
            queryset._before_cursor_values = self._cursor_values
            queryset._reverse_result_order = True
        queryset._cursor_values = values
        return queryset

    def cursor_values(self, obj: TModel) -> tuple[Any, ...]:
        """The keyset boundary values of ``obj`` for the current ``.order_by(...)`` - what
        ``.after_cursor()``/``.before_cursor()`` take. An ordering across a relation reads the
        related object off ``obj``; a missing relation gives ``None``.

        Args:
            obj: A row of this queryset's model.

        Returns:
            One value per ordering field.

        Raises:
            ValueError: ``.order_by()`` wasn't called, or a related ordering's relation isn't
                loaded.
            FieldError: An ordering field is an annotation, or crosses a to-many relation.
            QueryError: ``obj`` isn't of this queryset's model.
        """
        if not isinstance(obj, self.model):
            raise QueryError(f"cursor_values() expects a {self.model.__name__} instance, got {type(obj).__name__}")
        if not self._orderings:
            raise QueryError(".cursor_values() requires .order_by() to be called first")
        values = []
        for field_name, __ in self._orderings:
            if field_name in self._annotations:
                raise FieldError(f".cursor_values() does not support ordering by an annotation, got '{field_name}'")
            values.append(OrderingArguments.get_cursor_value(obj, field_name))
        return tuple(values)

    def _get_single_queryset(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        does_not_exist_exception: GetExceptionArgument,
        multiple_objects_returned_exception: GetExceptionArgument,
    ) -> Self:
        """The queryset of the one row matching the conditions.

        Args:
            args: ``Q`` conditions.
            kwargs: Filter keyword arguments.
            does_not_exist_exception: What is raised when no row matches - ``GetException.STANDARD``
                for ``DoesNotExist``, None to give None instead.
            multiple_objects_returned_exception: What is raised when more than one row matches -
                ``GetException.STANDARD`` for ``MultipleObjectsReturned``, None to read one row of
                them without counting.

        Returns:
            The single-row queryset.
        """
        if GenericForeignKeyFieldInstance.declared_names:
            args, kwargs = GenericForeignKeyFilters.rewrite_arguments(self.model, args, kwargs)
        get_call = CallSignatureRuns.get_get_call(self, kwargs) if kwargs and not args else None
        if get_call is not None:
            # Its queries run on the plan of the call; the filters are built only when the
            # queryset is built after all.
            queryset = self._clone_keeping_calls()
            queryset._filter_call_counter += 1
            queryset._pending_filter_calls = ((False, queryset._filter_call_counter, (), kwargs),)
            queryset._call_signature = (type(self), get_call)
            queryset._call_values = tuple(kwargs.values())
        else:
            filtered_queryset = self.filter(*args, **kwargs) if args or kwargs else self
            queryset = SingleRow.with_rows_as_subquery(filtered_queryset)
            filtered_queryset._continue_call_signature(queryset, ("single_row",))
        queryset._limit = 1 if multiple_objects_returned_exception is None else GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK
        queryset._single = True
        queryset._raise_does_not_exist = does_not_exist_exception is not None
        # Nothing to keep for the standard exceptions - the call left out of the common get().
        if (
            does_not_exist_exception is not GetException.STANDARD
            or multiple_objects_returned_exception is not GetException.STANDARD
            or queryset._options is not QueryOptions.DEFAULT
        ):
            GetExceptions.set_get_exceptions(queryset, does_not_exist_exception, multiple_objects_returned_exception)
        return queryset

    def _get_filter_value_query(self) -> ValuesQuery:
        """The query a filter compares with when this queryset is its value - the values it
        selects, else its primary key, as the filter resolves it.

        Returns:
            The query.
        """
        if self._options is not QueryOptions.DEFAULT and (rewritten := QueryRewrites.get_rewritten(self)) is not self:
            return rewritten._get_filter_value_query()
        if self._selection is not None:
            return StatementSelection.get_values_query(self)
        return self._get_field_values_query("pk")

    def _get_filter_value_compiler(self) -> AwaitableQuery[Any]:
        """The query a filter compares with when this queryset is its value, as the filter builds
        it: the values it selects, else its primary key - or the query combining its rows.

        Returns:
            The query - made again for each description and build, its values coming from this
            queryset.
        """
        self._build_conditions_for_copies()
        query = self._get_filter_value_query() if self._combination is None else self._get_compiler()
        query._plan_origin = self
        return query

    def _get_field_values_query(self, field_name: str) -> ValuesQuery:
        """One field of each row this queryset returns, as a query to embed - a queryset passed as
        a ``__in`` filter value.

        Args:
            field_name: The field to select, ``"pk"`` included.

        Returns:
            A flat ``values_list()`` query of the field.
        """
        if self._options is not QueryOptions.DEFAULT and (rewritten := QueryRewrites.get_rewritten(self)) is not self:
            return rewritten._get_field_values_query(field_name)
        return StatementSelection.get_values_query(
            StatementSelection.get_values_rows_queryset(self).values_list(field_name, flat=True)
        )

    @CallsBeforeSetup.recorded_result
    def latest(self, *orderings: str | Ordering) -> QuerySetSingle[TRow | None]:
        """Returns the most recent object - the first in the descending ordering by the given fields. A
        plain field name gets ``NULLS LAST``; an ``Ordering`` is reversed exactly.

        Args:
            orderings: Fields to order by - the model's ``Meta.get_latest_by`` when none is given.

        Raises:
            FieldError: A field is unknown, or none is given and the model has no ``Meta.get_latest_by``.
        """
        QuerySetCombination.raise_if_combined(self, "latest")
        SingleRow.raise_if_sliced_values_rows_differ(self, "latest")
        orderings = orderings or self.model._meta.get_latest_by
        if not orderings:
            raise FieldError(LATEST_WITHOUT_FIELDS_MESSAGE.format(method="latest"))
        OrderingArguments.forbid_reordering_after_cursor(self, "latest")
        queryset = SingleRow.with_rows_as_subquery(self)
        queryset._orderings = OrderingArguments.parse_orderings(
            self, orderings, reverse=True, nulls_last_by_default=True
        )
        return SingleRow.as_single(queryset)

    @CallsBeforeSetup.recorded_result
    def earliest(self, *orderings: str | Ordering) -> QuerySetSingle[TRow | None]:
        """Returns the earliest object - the first in the ascending ordering by the given fields. A
        plain field name gets ``NULLS LAST``; an ``Ordering`` is used as given.

        Args:
            orderings: Fields to order by - the model's ``Meta.get_latest_by`` when none is given.

        Raises:
            FieldError: A field is unknown, or none is given and the model has no ``Meta.get_latest_by``.
        """
        QuerySetCombination.raise_if_combined(self, "earliest")
        SingleRow.raise_if_sliced_values_rows_differ(self, "earliest")
        orderings = orderings or self.model._meta.get_latest_by
        if not orderings:
            raise FieldError(LATEST_WITHOUT_FIELDS_MESSAGE.format(method="earliest"))
        OrderingArguments.forbid_reordering_after_cursor(self, "earliest")
        queryset = SingleRow.with_rows_as_subquery(self)
        queryset._orderings = OrderingArguments.parse_orderings(self, orderings, nulls_last_by_default=True)
        return SingleRow.as_single(queryset)

    @CallsBeforeSetup.recorded
    def reverse(self) -> Self:
        """Reverses the ordering exactly - its explicit NULL placements too - the ordering given by
        ``order_by()``, else ``Meta.ordering``; an unordered queryset stays unordered.

        Raises:
            QueryError: The queryset is sliced or combined (``union()``...).
            ValueError: ``.after_cursor()`` was called - see ``order_by()``.
        """
        QuerySetCombination.raise_if_combined(self, "reverse")
        FilterArguments.raise_if_slice_taken(self, "Cannot reverse a query once a slice has been taken.")
        OrderingArguments.forbid_reordering_after_cursor(self, "reverse")
        queryset = self._clone()
        current = list(self._apply_default_ordering(self._orderings, self._annotations))
        queryset._orderings = [(field_name, order.get_reversed()) for field_name, order in current]
        return queryset

    def dates(self, field_name: str, trunc_type: str, order: str = "ASC") -> Self:
        """The distinct dates of a date or datetime field, truncated to ``type`` - a datetime's in the
        current zone - NULLs left out.

        Args:
            field_name: A ``DateField`` or ``DatetimeField`` of the model.
            trunc_type: ``year``, ``quarter``, ``month``, ``week`` or ``day``.
            order: ``ASC`` or ``DESC``.

        Returns:
            A ``values_list(flat=True)`` queryset of ``datetime.date`` values.

        Raises:
            FieldError: The field isn't a date or datetime field of the model, or ``type`` or
                ``order`` is unknown.
        """
        field = DateLists.get_dates_field(
            self, field_name, "dates", (DateField, DatetimeField), DATES_TRUNC_TYPES, trunc_type, order
        )
        truncated: Expression = Trunc(field_name, trunc_type)
        if isinstance(field, DatetimeField):
            truncated = Trunc(truncated, str(TruncType.DATE))
        return DateLists.get_distinct_dates(self, truncated, order)

    def datetimes(
        self, field_name: str, trunc_type: str, order: str = "ASC", tzinfo: str | TzInfo | None = None
    ) -> Self:
        """The distinct values of a datetime field, truncated to ``type`` in ``tzinfo`` (the current
        zone by default), NULLs left out.

        Args:
            field_name: A ``DatetimeField`` of the model.
            trunc_type: ``year``, ``quarter``, ``month``, ``week``, ``day``, ``hour``, ``minute`` or ``second``.
            order: ``ASC`` or ``DESC``.
            tzinfo: The zone the values are truncated in - an IANA name or a ``ZoneInfo``.

        Returns:
            A ``values_list(flat=True)`` queryset of ``datetime.datetime`` values.

        Raises:
            FieldError: The field isn't a datetime field of the model, or ``type`` or ``order`` is
                unknown.
            ConfigurationError: ``tzinfo`` isn't a known IANA zone.
        """
        DateLists.get_dates_field(
            self, field_name, "datetimes", (DatetimeField,), DATETIMES_TRUNC_TYPES, trunc_type, order
        )
        return DateLists.get_distinct_dates(self, Trunc(field_name, trunc_type, tzinfo=tzinfo), order)

    @CallsBeforeSetup.recorded
    def limit(self, limit: int) -> Self:
        """Sets the limit to the given value, replacing a limit already set - unlike slicing, which
        composes.

        Raises:
            QueryError: The limit is negative.
        """
        if limit < 0:
            raise QueryError("Limit should be non-negative number")

        queryset = self._clone()
        queryset._limit = limit
        return self._continue_call_signature(queryset, ("limit",))

    @CallsBeforeSetup.recorded
    def offset(self, offset: int) -> Self:
        """Sets the offset to the given value, replacing an offset already set - unlike slicing, which
        composes.

        Raises:
            QueryError: The offset is negative.
        """
        if offset < 0:
            raise QueryError("Offset should be non-negative number")

        queryset = self._clone()
        queryset._offset = offset
        return self._continue_call_signature(queryset, ("offset",))

    @overload
    def __getitem__(self, key: slice) -> Self: ...

    @overload
    def __getitem__(self, key: int) -> QuerySetSingle[TRow]: ...

    @CallsBeforeSetup.recorded_result
    def __getitem__(self, key: slice | int) -> QuerySet[TModel, TRow, TAnnotations] | QuerySetSingle[TRow]:
        """Slices the queryset, or takes the one object at an index. A slice composes with the offset
        and limit already set, like slicing a list: ``queryset[5:10][2:4]`` selects positions 7 and
        8. ``queryset[3]`` raises ``IndexError`` when awaited if there is no such row.

        Raises:
            QueryError: The key isn't a slice or a non-negative integer, the step isn't 1, or a
                bound is negative.
        """
        if isinstance(key, int) and not isinstance(key, bool):
            if key < 0:
                raise QueryError("Negative indexing is not supported.")
            single_queryset = self[key : key + 1]
            single_queryset._single = True
            single_queryset._is_single_row_of_slice = True
            single_queryset._raise_does_not_exist = True
            single_queryset._does_not_exist_exception = IndexError(f"Index {key} is out of range")
            return cast("QuerySetSingle[TRow]", single_queryset)
        if not isinstance(key, slice):
            raise QueryError("QuerySet indices must be slices or non-negative integers.")

        offset, limit = self._get_sliced_bounds(key, self._offset, self._limit)
        queryset = self.offset(offset)
        if limit is not None:
            queryset = queryset.limit(limit)
        return queryset

    @CallsBeforeSetup.recorded
    def distinct(self, *args: str) -> Self:
        """Makes the queryset return distinct rows. Without arguments it adds ``DISTINCT``; with field
        names it keeps one row per combination of them - an ``order_by()`` must then begin with the
        same fields: ``DISTINCT ON (fields)`` on PostgreSQL, ``ROW_NUMBER() ... = 1`` in a ``pk IN``
        subquery on a database without it.

        Example:
            ::

                await Tournament.objects.all().distinct().values("name")
                await Tournament.objects.all().distinct("name").order_by("name", "-desc")

        Args:
            args: Field names for ``DISTINCT ON`` - an annotation, or a path inside a field's value
                (``created__year``), too.

        Raises:
            QueryError: The queryset is sliced.
        """
        QuerySetCombination.raise_if_combined(self, "distinct")
        FilterArguments.raise_if_not_names(self, args, "distinct")
        FilterArguments.raise_if_slice_taken(self, "Cannot create distinct fields once a slice has been taken.")
        queryset = self._clone_keeping_calls()
        # A path into a JSON field or a date part (``created__year``) is an alias of its own, never
        # selected.
        path_annotations = AnnotationArguments.get_path_annotations(self, args)
        if path_annotations:
            AnnotationArguments.detach_annotation_descriptions(queryset, path_annotations)
            queryset._annotations = {**queryset._annotations, **path_annotations}
            queryset._alias_keys = queryset._alias_keys | set(path_annotations)
        queryset._distinct = True
        queryset._distinct_on = list(args)
        return self._continue_call_signature(queryset, ("distinct", args))

    @CallsBeforeSetup.recorded
    def union(self, *other_querysets: QuerySet[Any, Any], all: bool = False) -> Self:
        """Returns the queryset of the combined rows (``UNION``). Chained set operations combine each
        new queryset with the whole result so far. Model querysets combine with model querysets,
        values querysets with values querysets - the rows take the shape of the first one.

        Args:
            other_querysets: The querysets to combine with.
            all: Keep duplicate rows (``UNION ALL``).

        Raises:
            QueryError: Model instances are combined with ``.values()``/``.values_list()`` rows.
        """
        return QuerySetCombination.combine(
            self, other_querysets, SetOperation.UNION_ALL if all else SetOperation.UNION
        )

    @CallsBeforeSetup.recorded
    def intersection(self, *other_querysets: QuerySet[Any, Any]) -> Self:
        """
        Return the intersection of QuerySets - the rows every queryset returns (SQL INTERSECT).

        Args:
            other_querysets: Another QuerySet(s) to intersect with.

        Returns:
            The queryset of the combined rows.

        Raises:
            QueryError: Model instances are combined with ``.values()``/``.values_list()`` rows.
        """
        return QuerySetCombination.combine(self, other_querysets, SetOperation.INTERSECT)

    @CallsBeforeSetup.recorded
    def difference(self, *other_querysets: QuerySet[Any, Any]) -> Self:
        """
        Return the difference of QuerySets - this queryset's rows no other one returns (SQL EXCEPT).

        Args:
            other_querysets: Another QuerySet(s) to subtract.

        Returns:
            The queryset of the combined rows.

        Raises:
            QueryError: Model instances are combined with ``.values()``/``.values_list()`` rows.
        """
        return QuerySetCombination.combine(self, other_querysets, SetOperation.EXCEPT_OF)

    @staticmethod
    def _raise_sample_write(method_name: str) -> None:
        """Rejects a write on a queryset reading a sample of its table - ``TABLESAMPLE`` is a part of
        ``SELECT`` only. Called only for a queryset with ``sample()``.

        Args:
            method_name: The method.

        Raises:
            QueryError: Always.
        """
        raise QueryError(
            f"{method_name}() can't be used on a sample() - write the sampled rows by their keys: "
            f"Model.objects.filter(pk__in=queryset.sample(...)).{method_name}(...)"
        )

    @CallsBeforeSetup.recorded
    def sample(
        self, percent: float, *, method: TableSampleMethod | str = TableSampleMethod.BERNOULLI, seed: int | None = None
    ) -> Self:
        """Reads a random sample of the model's table instead of all of it - ``TABLESAMPLE``: each row
        (``BERNOULLI``) or each storage block with its rows (``SYSTEM``) is kept with ``percent``
        chance, before the queryset's filters apply. The same ``seed`` reads the same sample while the
        table doesn't change.

        Args:
            percent: The chance, in percent - an int or float from 0 to ``MAX_TABLE_SAMPLE_PERCENT``.
            method: ``TableSampleMethod.BERNOULLI`` or ``SYSTEM``, or its name in any case.
            seed: None for a new sample each run, or an int from 0 to ``MAX_TABLE_SAMPLE_SEED``.

        Returns:
            The queryset reading the sample.

        Raises:
            QueryError: An argument is of another type or out of its range; the queryset is combined.
            UnSupportedError: When run - the database has no ``TABLESAMPLE``.
        """
        QuerySetCombination.raise_if_combined(self, "sample")
        table_sample = TableSample.build(percent, method, seed)
        queryset = self._clone()
        queryset._table_sample = table_sample
        return queryset

    @CallsBeforeSetup.recorded
    def with_cte(self, name: str, query: AwaitableQuery[Any] | Selectable) -> Self:
        """Attaches a named ``WITH`` to the queryset's SQL - recursive when ``query`` references its
        own name. The queryset still selects from the model's table; its rows are read from the CTE
        by a filter (``id__in=CteRows(name, "id")``), a ``Subquery`` or hand-built SQL. A walk along a
        relation of the model to itself is ``with_recursive()``.

        A hand-built ``Selectable`` doesn't get the model's row scope
        (``Meta.soft_delete_field``/``Meta.tenant_field``): a recursive step without it walks
        through a hidden row. Add ``RowScopes.of(model).get_criterion(...)`` to the base case and to
        the step.

        Args:
            name: The CTE's name.
            query: A queryset, or a ``hare.sql`` ``Selectable`` built with the ``query_class`` of
                the connection the query runs on.

        Example:
            ::

                connection = Category.get_connection()
                query_class = connection.query_class
                category, ancestors = Table("category"), Table("ancestors")
                scope = RowScopes.of(Category).get_criterion(
                    category, dialect=connection.dialect, connection=connection
                )
                base_where = category.id == root_id
                step_where = category.id == ancestors.parent_id
                if scope is not None:
                    base_where, step_where = base_where & scope, step_where & scope
                columns = (category.id, category.parent_id)
                base = query_class.from_(category).select(*columns).where(base_where)
                step = query_class.from_(category).join(ancestors).on(step_where).select(*columns)
                union_all = base * step
                union_all.base_query.wrap_set_operation_queries = False
                await (
                    Category.objects.all()
                    .using(connection)
                    .with_cte("ancestors", union_all)
                    .filter(id__in=CteRows("ancestors", "id"))
                )
        """
        QuerySetCombination.raise_if_combined(self, "with_cte")
        queryset = self._clone()
        # A queryset selecting values is built by its values query.
        cte_body = query._get_compiler() if isinstance(query, QuerySpecification) else query
        queryset._with_ctes = (*queryset._with_ctes, (name, cte_body))
        return queryset

    def with_recursive(self, relation: str, *, max_depth: int | None = None) -> QuerySet[TModel, TModel]:
        """The rows reachable from this queryset's rows through ``relation`` - a relation of the model
        to itself - followed again and again, these rows included, in one ``WITH RECURSIVE`` query:
        ``Category.objects.filter(pk=root.pk).with_recursive("children")`` is the root and every
        descendant, ``with_recursive("parent")`` a row and its ancestors. A row reached twice comes
        once, so a cycle ends; the rows the model's default scope hides aren't walked through.

        Args:
            relation: The relation - forward, reverse or many-to-many.
            max_depth: How many steps from this queryset's rows - 0 for them alone; None for no
                limit.

        Returns:
            A queryset of the model's rows reached - on this queryset's connection and with its
            visibility, otherwise unfiltered, to filter, order and read like any other.

        Raises:
            QueryError: ``max_depth`` isn't None or an int in ``0..MAX_RECURSIVE_DEPTH``; the
                queryset is combined (``union()``...).
            FieldError: When run - ``relation`` isn't a relation of the model to itself.
        """
        QuerySetCombination.raise_if_combined(self, "with_recursive")
        reachable_keys = RecursiveRows(self, relation, max_depth=max_depth)
        self.model._meta.raise_if_no_primary_key("with_recursive()")
        rows = cast("QuerySet[TModel, TModel]", self.model._meta.manager.get_queryset())
        rows._apply_connection(self._connection)
        rows._connection_explicitly_chosen = self._connection_explicitly_chosen
        rows._visibility = self._visibility
        primary_key_attribute = self.model._meta.primary_key_attribute
        key_filter = "pk__in" if isinstance(primary_key_attribute, tuple) else f"{primary_key_attribute}__in"
        return rows.filter(**{key_filter: reachable_keys})

    @CallsBeforeSetup.recorded
    def select_for_update(
        self,
        *,
        nowait: bool = False,
        skip_locked: bool = False,
        of: Sequence[str] = (),
        no_key: bool = False,
        share: bool = False,
        key_share: bool = False,
    ) -> Self:
        """
        Make QuerySet select for update.

        Returns a queryset that will lock rows until the end of the transaction,
        generating a SELECT ... FOR UPDATE SQL statement on supported databases.

        Args:
            share: If `True`, take the shared ``FOR SHARE`` lock - other transactions may lock the
                rows the same way, none may update or delete them.
            key_share: If `True`, take the weakest ``FOR KEY SHARE`` lock - it keeps the rows from
                being deleted or having their key changed only.
            nowait: If `True`, raise an error if the lock cannot be obtained immediately.
            skip_locked: If `True`, skip rows that are already locked by other transactions
                instead of waiting.
            of: What to lock when the query joins related tables: ``"self"`` (or the model's own
                table name) for the model's rows, and a forward relation path as passed to
                ``select_related()`` (``"author"``, ``"author__country"``) for a joined model's
                rows. A named relation is joined with INNER JOIN, so every hop of its path must be
                a NOT NULL foreign key with a database constraint and no soft delete/tenant/
                ``Select(extra_condition=...)`` condition. By default, all fetched rows are
                locked - only the model's own rows when the query joins any relation.
            no_key: If `True`, use the lower SELECT ... FOR NO KEY UPDATE lock strength on
                PostgreSQL to allow creating or deleting rows in other tables that reference the
                locked rows via foreign keys. The parameter is ignored on other backends.

        Raises:
            QueryError: A flag isn't a bool; ``nowait`` and ``skip_locked``, or more than one of
                ``no_key``, ``share`` and ``key_share``, are set; or, when the query is built, an
                ``of`` name is not a forward relation path, isn't joined by the query, or crosses a
                relation that can't be INNER JOINed.
            UnSupportedError: when the query runs, the database has no SELECT ... FOR UPDATE
                equivalent, or no ``FOR SHARE``/``FOR KEY SHARE`` asked for.
        """
        QuerySetCombination.raise_if_combined(self, "select_for_update")
        flags = {
            "nowait": nowait,
            "skip_locked": skip_locked,
            "no_key": no_key,
            "share": share,
            "key_share": key_share,
        }
        if wrong_flags := [name for name, value in flags.items() if not isinstance(value, bool)]:
            raise QueryError(f"select_for_update() takes bools for {', '.join(wrong_flags)}")
        if nowait and skip_locked:
            raise QueryError("select_for_update() options nowait and skip_locked are mutually exclusive")
        strengths = [
            strength
            for strength, chosen in (
                (RowLockStrength.NO_KEY_UPDATE, no_key),
                (RowLockStrength.SHARE, share),
                (RowLockStrength.KEY_SHARE, key_share),
            )
            if chosen
        ]
        if len(strengths) > 1:
            raise QueryError("select_for_update() takes one of no_key, share and key_share")
        if isinstance(of, str):
            raise QueryError(
                f"select_for_update(of=...) takes a tuple of paths, got the string {of!r} - wrap a single path "
                "in a tuple"
            )
        queryset = self._clone()
        queryset._select_for_update = True
        queryset._select_for_update_nowait = nowait
        queryset._select_for_update_skip_locked = skip_locked
        queryset._select_for_update_of = set(of)
        queryset._select_for_update_strength = strengths[0] if strengths else None
        return queryset

    @CallsBeforeSetup.recorded
    def annotate(self, **kwargs: Expression | Term) -> Self:
        """
        Annotate result with aggregation or function result.

        Raises:
            TypeError: Value of kwarg is expected to be a ``Function`` instance.
            FieldError: A key is a field of the model or ``pk``.
        """
        QuerySetCombination.raise_if_combined(self, "annotate")
        kwargs = AnnotationArguments.get_annotations(kwargs)
        relation_aliases = {key: value for key, value in kwargs.items() if isinstance(value, NamedJoin)}
        if relation_aliases:
            # A named JOIN (FilteredRelation, Lateral) is never a selected value - annotated, it is an alias.
            other_annotations = {key: value for key, value in kwargs.items() if key not in relation_aliases}
            aliased = self.alias(**relation_aliases)
            return aliased.annotate(**other_annotations) if other_annotations else aliased
        AnnotationArguments.raise_if_annotation_names_collide_with_fields(self, kwargs)
        queryset = self._clone_keeping_calls()
        AnnotationArguments.detach_annotation_descriptions(queryset, kwargs)
        for key, annotation in kwargs.items():
            queryset._annotations[key] = annotation
            # A previous .alias(key=...) on this same key is superseded by a real .annotate() -
            # the key should now behave like an ordinary annotation (selected by default).
            queryset._alias_keys = queryset._alias_keys - {key}
        if queryset._selection is not None:
            queryset._selection = queryset._selection.with_added_names(kwargs)
        return self._continue_annotation_call_signature(queryset, "annotate", kwargs)

    @CallsBeforeSetup.recorded
    def alias(self, **kwargs: Expression | Term) -> Self:
        """
        Like ``.annotate()`` - usable in ``.filter()``/``.order_by()`` - but the expression is
        never added to the SELECT list on its own, only when explicitly requested via
        ``.values()``/``.values_list()``.

        Raises:
            TypeError: Value of kwarg is expected to be a ``Function`` instance.
            FieldError: A key is a field of the model or ``pk``.
        """
        QuerySetCombination.raise_if_combined(self, "alias")
        kwargs = AnnotationArguments.get_annotations(kwargs)
        AnnotationArguments.raise_if_annotation_names_collide_with_fields(self, kwargs)
        queryset = self._clone_keeping_calls()
        AnnotationArguments.detach_annotation_descriptions(queryset, kwargs)
        for key, annotation in kwargs.items():
            queryset._annotations[key] = annotation.with_name(key) if isinstance(annotation, NamedJoin) else annotation
            queryset._alias_keys = queryset._alias_keys | {key}
        if queryset._selection is not None:
            queryset._selection = queryset._selection.with_added_names((), kwargs)
        return self._continue_annotation_call_signature(
            queryset, "alias", {key: queryset._annotations[key] for key in kwargs}
        )

    def _continue_annotation_call_signature(self, queryset: Self, call_name: str, annotations: dict[str, Any]) -> Self:
        """Gives a queryset ``annotate()``/``alias()`` made this one's call signature with the call
        appended, when each annotation describes itself for a plan - the expressions are the call's
        values, each described by its own description; else the queryset has no call signature.

        Args:
            queryset: The queryset made.
            call_name: The call.
            annotations: The annotations it added, by key.

        Returns:
            ``queryset``.
        """
        if self._call_signature is None:
            return queryset
        for annotation in annotations.values():
            if not isinstance(annotation, Plannable):
                queryset._call_signature = None
                return queryset
        return self._continue_call_signature(queryset, (call_name, tuple(annotations)), tuple(annotations.values()))

    @CallsBeforeSetup.recorded
    def group_by(self, *fields: str | GroupingSet) -> Self:
        """
        Make QuerySet returns list of dict or tuple with group by - before or after
        ``.values()``/``.values_list()``. One ``Rollup``/``Cube``/``GroupingSets`` among the fields groups
        the rows several ways at once - the plain fields given beside it are in every grouping.

        Raises:
            QueryError: More than one ``Rollup``/``Cube``/``GroupingSets`` is given.
        """
        QuerySetCombination.raise_if_combined(self, "group_by")
        grouping_sets = [field for field in fields if isinstance(field, GroupingSet)]
        if len(grouping_sets) > 1:
            raise QueryError("group_by() takes one Rollup, Cube or GroupingSets")
        grouping_set = grouping_sets[0] if grouping_sets else None
        plain_names = tuple(field for field in fields if isinstance(field, str))
        queryset = self._clone_keeping_calls()
        queryset._group_bys = (
            plain_names if grouping_set is None else tuple(dict.fromkeys((*plain_names, *grouping_set.field_names)))
        )
        queryset._grouping_set = grouping_set
        return self._continue_call_signature(
            queryset, ("group_by", plain_names, None if grouping_set is None else grouping_set.get_plan_key())
        )

    @overload
    def values_list(
        self, *fields_: str, flat: Literal[False] = False, named: Literal[False] = False, **kwargs: Expression | Term
    ) -> QuerySet[TModel, tuple[Any, ...], TAnnotations]: ...

    @overload
    def values_list(
        self, *fields_: str, flat: bool = False, named: bool = False, **kwargs: Expression | Term
    ) -> QuerySet[TModel, Any, TAnnotations]: ...

    @CallsBeforeSetup.recorded
    def values_list(
        self, *fields_: str, flat: bool = False, named: bool = False, **kwargs: Expression | Term
    ) -> QuerySet[TModel, Any, TAnnotations]:
        """Makes the queryset return tuples of the given fields - all fields in declaration order when
        none are given. A kwarg selects an expression under its key, after the positional names. A
        name may read inside a field's value, as in ``.values()``. On a queryset already selecting
        values, it selects other fields instead.

        Args:
            fields_: Field names.
            flat: Return the single selected value instead of a one-item tuple.
            named: Return namedtuples; a name that isn't an identifier becomes ``_<index>``.
            kwargs: Expressions by key.

        Raises:
            QueryError: ``flat`` and ``named`` are both set, ``flat`` with other than one name, a
                positional argument isn't a field name, or a kwarg isn't an expression.
        """
        if self._combination is not None:
            return CombinedDerivedQueries.get_values_queryset(  # type: ignore[return-value]
                QuerySetCombination.get_combined_query(self),
                lambda branch: branch.values_list(*fields_, flat=flat, named=named, **kwargs),
            )
        if flat and named:
            raise QueryError(".values_list() can't use flat=True and named=True together.")
        if GenericForeignKeyFieldInstance.declared_names and (
            type_aliases := GenericForeignKeyTypes.get_pending_type_aliases(self, fields_)
        ):
            return self.alias(**type_aliases).values_list(*fields_, flat=flat, named=named, **kwargs)
        queryset = self._get_values_source_queryset()
        FilterArguments.check_selected_names(queryset, fields_, "values_list")
        ValuesArguments.raise_if_values_unusable(
            queryset, ".values_list()", fields_, kwargs, allow_field_name_kwargs=False
        )
        names = (*fields_, *kwargs)
        if flat and len(names or ValuesArguments.get_every_selected_name(queryset)) != 1:
            raise QueryError(".values_list(flat=True) selects exactly one field")
        queryset = queryset.alias(**kwargs) if kwargs else queryset._clone()
        shape = RowShape.FLAT if flat else RowShape.NAMED if named else RowShape.TUPLE
        queryset._selection = ValuesSelection(shape, field_names=names)
        if self._selection is None and not kwargs:
            self._continue_call_signature(queryset, ("values_list", fields_, flat, named))
        return queryset  # type: ignore[return-value]

    @CallsBeforeSetup.recorded
    def values(self, *args: str, **kwargs: str | Expression | Term) -> QuerySet[TModel, dict[str, Any], TAnnotations]:
        """Makes the queryset return dicts - of all fields when no arguments are given. A kwarg renames
        a field (``key=field_name``) or selects an expression under its key. A name may read inside
        a field's value, through relations too: a JSON key path, an array item or range bound, or a
        part of a date, time or datetime. On a queryset already selecting values, it selects other
        fields instead.

        Raises:
            FieldError: A key is given twice, or an expression kwarg is named after a field of the
                model.
            QueryError: A positional argument isn't a field name, or a kwarg is neither a field name
                nor an expression.
        """
        if self._combination is not None:
            return CombinedDerivedQueries.get_values_queryset(  # type: ignore[return-value]
                QuerySetCombination.get_combined_query(self), lambda branch: branch.values(*args, **kwargs)
            )
        if GenericForeignKeyFieldInstance.declared_names and (
            type_aliases := GenericForeignKeyTypes.get_pending_type_aliases(self, args)
        ):
            return self.alias(**type_aliases).values(*args, **kwargs)
        queryset = self._get_values_source_queryset()
        FilterArguments.check_selected_names(queryset, args, "values")
        ValuesArguments.raise_if_values_unusable(queryset, ".values()", args, kwargs, allow_field_name_kwargs=True)
        selected_keys: set[str] = set()
        for key in (*args, *kwargs):
            if key in selected_keys:
                raise FieldError(f"Duplicate key {key}")
            selected_keys.add(key)
        # An expression is an alias of the queryset under its key; so is a renamed field
        # (values(k="name")) - a later filter, ordering or aggregate reads either by that name. A new
        # name that is a field or an annotation of the model keeps meaning that one.
        aliases: dict[str, Expression | Term] = {
            key: value for key, value in kwargs.items() if not isinstance(value, str)
        }
        aliases.update(
            (key, F(value))
            for key, value in kwargs.items()
            if isinstance(value, str)
            and key not in (value, "pk")
            and key not in self.model._meta.fields_map
            and key not in queryset._annotations
        )
        queryset = queryset.alias(**aliases) if aliases else queryset._clone()
        queryset._selection = ValuesSelection(
            RowShape.DICT,
            field_names=args,
            renamed_fields=tuple((key, value if isinstance(value, str) else key) for key, value in kwargs.items()),
        )
        if self._selection is None and not kwargs:
            self._continue_call_signature(queryset, ("values", args))
        return queryset  # type: ignore[return-value]

    def _get_values_source_queryset(self) -> QuerySet[TModel, TModel]:
        """The queryset a ``.values()``/``.values_list()`` call selects from - this one, or, when it
        already selects values, this one selecting model instances again, explicitly grouped by the
        selected fields an aggregate implicitly groups it by.

        Returns:
            The queryset.
        """
        if self._selection is None:
            return self  # type: ignore[return-value]
        return ValuesGrouping.get_grouped_source_queryset(StatementSelection.get_values_query(self))

    def _with_selection(self, selection: ValuesSelection | None) -> QuerySet[TModel, Any]:
        """A clone returning its rows as ``selection`` selects them.

        Args:
            selection: What the rows select, None for model instances.

        Returns:
            The clone.
        """
        queryset = self._clone()
        queryset._selection = selection
        return cast("QuerySet[TModel, Any]", queryset)

    def _get_compiler(self) -> AwaitableQuery[Any]:
        """The query building this queryset's SQL - the queryset itself for model instances.

        Returns:
            The query.
        """
        CallsBeforeSetup.raise_if_invalid(self)
        return StatementSelection.get_select_query(self)

    def _raise_if_prefetching(self, method_name: str, result_description: str) -> None:
        """Rejects a method returning no instances on a query prefetching relations.

        Args:
            method_name: The method.
            result_description: What the method returns instead.

        Raises:
            QueryError: ``prefetch_related()`` was called.
        """
        if self._prefetch_map or self._prefetch_queries:
            raise QueryError(
                f"{method_name}() cannot be used with prefetch_related() - the result is {result_description}, "
                "not model instances, so there's nothing to attach a prefetched relation to."
            )

    def _get_count_query(self) -> CountQuery:
        """The number of rows - counted as returned when a primary key can repeat.

        Raises:
            QueryError: ``prefetch_related()`` was called.
        """
        self._raise_if_prefetching("count", "a plain int")
        rows_query = StatementSelection.get_rows_query_if_rows_repeat(self)
        count_query = CountQuery(self, rows_query=rows_query)
        if rows_query is None:
            self._hand_call_signature(count_query)
        return count_query

    def _get_exists_query(self) -> ExistsQuery:
        """Whether any row matches.

        Raises:
            QueryError: ``prefetch_related()`` was called.
        """
        self._raise_if_prefetching("exists", "a plain bool")
        # Without an offset the repeated rows can't change whether any row exists - a call of the
        # dialect's QuerySet method changing the rows can.
        rows_query = (
            StatementSelection.get_rows_query_if_rows_repeat(self)
            if self._offset or (self._extension_calls and QuerySetExtensions.changes_rows(self))
            else None
        )
        exists_query = ExistsQuery(self, rows_query=rows_query)
        if rows_query is None:
            self._hand_call_signature(exists_query)
        return exists_query

    def _get_aggregate_query(self, **kwargs: Expression | Term) -> AggregateQuery:
        """Aggregates over the rows - of a slice, restricted to them by their primary key.

        Raises:
            QueryError: The query is sliced and its rows can repeat a primary key.
            QueryError: ``prefetch_related()`` was called.
        """
        if self._limit is not None or self._offset:
            if RowMultiplication.rows_repeat_per_primary_key(ModelRowsQuery(self)) or (
                not self._distinct_on and StatementSelection.get_rows_query_if_rows_repeat(self) is not None
            ):
                raise QueryError(
                    "aggregate() can't be used on a sliced queryset whose rows can repeat a primary key (a filter, "
                    "annotation or ordering over a to-many relation) - the slice would hold repeated rows the "
                    "aggregate can't tell apart. Aggregate a sliced .values()/.values_list() query instead."
                )
            # An unordered slice holds the first rows by primary key, the rows first() reads.
            rows_queryset = SingleRow.with_rows_as_subquery(
                OrderingArguments.with_primary_key_ordering_if_unordered_slice(self)
            )
            # A .distinct() slice's filters can still join a to-many relation: its rows are then
            # reduced to one per primary key before the metrics run, as an unsliced .distinct().
            rows_queryset._distinct = self._distinct and not self._distinct_on
            return rows_queryset.aggregate(**kwargs)
        self._raise_if_prefetching("aggregate", "a plain dict")
        aggregate_query = AggregateQuery(self, kwargs)
        self._hand_call_signature(aggregate_query)
        return aggregate_query

    def _get_update_query(self, **kwargs: Any) -> UpdateQuery:
        """Updates the rows.

        Raises:
            QueryError: ``Meta.soft_delete_field`` or ``Meta.optimistic_lock_field`` is among
                ``kwargs``.
        """
        if self.model._meta.soft_delete_field in kwargs:
            raise QueryError(
                f"Cannot set '{self.model._meta.soft_delete_field}' via .update() - use .delete()/.restore() instead"
            )
        if self.model._meta.optimistic_lock_field in kwargs:
            raise QueryError(
                f"Cannot set '{self.model._meta.optimistic_lock_field}' via .update() - it's bumped automatically"
            )
        update_query = UpdateQuery(self, kwargs)
        self._hand_call_signature(update_query)
        return update_query

    def _get_first(self, *, reverse: bool) -> QuerySetSingle[TRow | None]:
        """The first (or last) row - of the ordering, else of the primary key. The last row
        reverses the ordering exactly; an unordered slice takes its rows by the primary key.

        Args:
            reverse: Take the last row.

        Returns:
            The single-row queryset.

        Raises:
            QueryError: ``last()`` after ``after_cursor()``/``before_cursor()``.
        """
        if not reverse:
            queryset = self._clone()
            if (
                not self._apply_default_ordering(self._orderings, self._annotations)
                and not self._group_bys
                and not self._distinct_on
            ):
                queryset._orderings = [
                    (primary_key_attribute_name, Order.ASC)
                    for primary_key_attribute_name in self.model._meta.primary_key_attribute_names
                ]
            return SingleRow.as_single(queryset)
        OrderingArguments.forbid_reordering_after_cursor(self, "last")
        queryset = SingleRow.with_rows_as_subquery(
            OrderingArguments.with_primary_key_ordering_if_unordered_slice(self)
        )
        effective_orderings = self._apply_default_ordering(queryset._orderings, queryset._annotations)
        if effective_orderings:
            queryset._orderings = [(field, order_type.get_reversed()) for field, order_type in effective_orderings]
        else:
            queryset._orderings = [
                (primary_key_attribute_name, Order.DESC)
                for primary_key_attribute_name in self.model._meta.primary_key_attribute_names
            ]
        return SingleRow.as_single(queryset)

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Describes this query built into another one.

        Args:
            context: The enclosing query's - this query resolves names against its own
                annotations.

        Returns:
            The description, None for a query that keeps no plan.
        """
        if self._combination is not None:
            return QuerySetCombination.get_combined_query(self).get_plan_description(context)
        if self._selection is not None:
            return StatementSelection.get_values_query(self).get_plan_description(context)
        return ModelRowsQuery(self).get_plan_description(context)

    @CallsBeforeSetup.recorded_result
    def delete(self) -> DeleteQuery:
        """
        Delete all objects in QuerySet.

        Raises:
            QueryError: The queryset selects values - call ``delete()`` before ``.values()``, like Django.
        """
        QuerySetCombination.raise_if_combined(self, "delete")
        if self._table_sample is not None:
            self._raise_sample_write("delete")
        ValuesArguments.raise_if_values_selected(self, "delete")

        return DeleteQuery(self)

    @CallsBeforeSetup.recorded_result
    def hard_delete(self) -> HardDeleteQuery:
        """Deletes every matched row for real, even with ``Meta.soft_delete_field`` - a soft-deleted
        row too, when the queryset sees it (``only_deleted().hard_delete()``). Related rows follow
        their ``on_delete``.

        Raises:
            QueryError: The queryset selects values.
        """
        QuerySetCombination.raise_if_combined(self, "hard_delete")
        if self._table_sample is not None:
            self._raise_sample_write("hard_delete")
        ValuesArguments.raise_if_values_selected(self, "hard_delete")

        return HardDeleteQuery(self)

    def restore(self, *, cascade: bool = False) -> RestoreQuery:
        """Reverses the soft delete of the queryset's rows - of those among them that are
        soft-deleted, whatever visibility the queryset has (``Model.objects.filter(...).restore()``
        reads the deleted rows). Awaits to the number of rows restored.

        Args:
            cascade: Also restore the rows the soft delete of each removed along with it - those still
                carrying its deletion time, as ``Model.restore(cascade=True)``.

        Returns:
            The restore.

        Raises:
            QueryError: The model has no ``Meta.soft_delete_field``; the queryset selects values, is
                combined or reads a sample; ``cascade`` isn't a bool.
        """
        QuerySetCombination.raise_if_combined(self, "restore")
        if self._table_sample is not None:
            self._raise_sample_write("restore")
        ValuesArguments.raise_if_values_selected(self, "restore")
        return RestoreQuery(self, cascade)

    @CallsBeforeSetup.recorded_result
    def update(self, **kwargs: Any) -> UpdateQuery:
        """
        Update all objects in QuerySet with given kwargs.

        Example:
            ::

                await Employee.objects.filter(occupation="developer").update(salary=5000)

        Will instead of returning a resultset, update the data in the DB itself.

        Raises:
            QueryError: If ``Meta.soft_delete_field`` is among ``kwargs`` - use
                ``.delete()`` (which applies cascade logic a direct field update would bypass) or
                ``.restore()`` (``cascade=True`` brings back what the delete cascaded) instead. Also raised if
                ``Meta.optimistic_lock_field`` is among ``kwargs`` - it's bumped automatically on every
                ``.update()`` call, not set directly. Also raised
                if ``Meta.tenant_field`` is among ``kwargs`` with a value outside the model's
                tenant scope (``Tenancy.scope()``), with an expression, or on a queryset with no
                tenant scope (``.all_tenants()`` included) - rows move only between the tenants
                the scope allows.
            QueryError: The queryset selects values whose rows are groups (an aggregate annotation
                or ``.group_by()``), or is sliced over rows that aren't one per model row - which
                model rows to update would be ambiguous. Otherwise the selected fields don't change
                which rows are updated, like Django.
        """
        if self._table_sample is not None:
            self._raise_sample_write("update")
        if self.model._meta.generic_foreign_key_fields:
            kwargs = GenericForeignKeys.expand_kwargs(self.model._meta, kwargs, apply_defaults=False)
        if self.model._meta.blind_index_fields:
            kwargs = BlindIndexes.expand_update_values(self.model._meta, kwargs)
        return StatementSelection.get_rows_source(self)._get_update_query(**kwargs)

    @CallsBeforeSetup.recorded_result
    def count(self) -> CountQuery:
        """
        Return count of objects in queryset instead of objects.

        Raises:
            ValueError: If prefetch_related() was called earlier in the chain.
        """
        return StatementSelection.get_rows_source(self)._get_count_query()

    @CallsBeforeSetup.recorded_result
    def aggregate(self, **kwargs: Expression | Term) -> AggregateQuery:
        """Computes aggregate expressions (``Sum``, ``Avg``, ``Count``, ...) across the whole queryset
        in one query and returns one dict keyed by the kwargs. After ``.group_by()`` they run over
        the grouped rows and read only the group-by fields and annotations. A sliced queryset
        aggregates the rows of its slice.

        Example:
            ::

                await Event.objects.filter(tournament=tournament).aggregate(total=Count("event_id"), avg=Avg("id"))

        Raises:
            QueryError: The queryset is sliced and its rows can repeat a primary key, or is
                ``.distinct(<fields>)``.
            ValueError: prefetch_related() was called earlier in the chain.
        """
        if self._distinct_on:
            # Checked before a database without DISTINCT ON turns the fields into a condition.
            raise QueryError(AGGREGATE_OVER_DISTINCT_ON_MESSAGE)
        if self._grouping_set is not None:
            raise QueryError(AGGREGATE_OVER_GROUPING_SETS_MESSAGE.format(grouping_set=self._grouping_set))
        return StatementSelection.get_rows_source(self)._get_aggregate_query(**kwargs)

    @CallsBeforeSetup.recorded_result
    def exists(self) -> ExistsQuery:
        """
        Return True/False whether queryset exists.

        Raises:
            ValueError: If prefetch_related() was called earlier in the chain.
        """
        return StatementSelection.get_rows_source(self)._get_exists_query()

    @CallsBeforeSetup.recorded_result
    def contains(self, obj: TModel) -> ContainsQuery:
        """
        Check if the QuerySet contains the given instance.

        Args:
            obj: The model instance to check for.

        Returns:
            True if the QuerySet contains the instance, False otherwise.
        """
        QuerySetCombination.raise_if_combined(self, "contains")
        ValuesArguments.raise_if_values_selected(self, "contains")
        if not isinstance(obj, self.model):
            raise QueryError("The given object is not an instance of the queryset's model.")

        if obj._pk_is_unset():
            raise QueryError("The given object does not have a primary key.")

        if self._limit is not None or self._offset:
            # Membership in a SLICE needs the object's position among the ordered rows - a plain
            # `WHERE pk = ...` has no way to express that, and silently ignoring the slice made
            # contains() answer for the whole table instead (limit(0).contains(obj) was True).
            raise QueryError("contains() can't be used on a sliced queryset (after .limit()/.offset()).")

        return ContainsQuery(self, obj)

    @CallsBeforeSetup.recorded
    def all(self) -> Self:
        """
        Return the whole QuerySet.
        Essentially a no-op except as the only operation.
        """
        return self._continue_call_signature(self._clone(), None)

    @CallsBeforeSetup.recorded
    def none(self) -> Self:
        """Returns an empty queryset: awaiting it, or anything chained onto it, gives an empty result
        without a query. Only ``.aggregate()`` still runs, with an always-false condition, so each
        expression gets its value for an empty set.
        """
        queryset = self._clone()
        queryset._is_none = True
        return queryset

    def raw(self, sql: str, parameters: Sequence[Any] = ()) -> RawSQLQuery[TModel]:
        """
        Return the QuerySet from raw SQL. Any value that varies per call belongs in ``parameters``
        (substituted at each ``%s`` placeholder in ``sql`` as a real bind parameter), never
        string-interpolated into ``sql`` directly - see ``RawSQL``'s own docstring.
        """
        # The SQL is the caller's own - no default scope is worked out for it.
        connection = self._connection if self._connection is not None else self.get_connection()
        return RawSQLQuery(model=self.model, connection=connection, sql=sql, parameters=parameters)

    @CallsBeforeSetup.recorded_result
    def first(self) -> QuerySetSingle[TRow | None]:
        """Limits the queryset to its first object and returns it instead of a list. Without an
        ordering the rows are ordered by the primary key; a ``.group_by()``/``.distinct(<fields>)``
        queryset is left unordered.
        """
        rows_source = StatementSelection.get_rows_source(self)
        first_queryset = rows_source._get_first(reverse=False)
        if rows_source is self:
            self._continue_call_signature(first_queryset, ("first",))  # type: ignore[type-var]
        return first_queryset  # type: ignore[return-value]

    @CallsBeforeSetup.recorded_result
    def last(self) -> QuerySetSingle[TRow | None]:
        """Limits the queryset to its last object and returns it instead of a list. The ordering is
        reversed exactly, NULL placement included. The rows of an unordered slice are taken by the
        primary key.

        Raises:
            QueryError: The queryset selects values and is sliced over rows that aren't one per
                model row.
        """
        rows_source = StatementSelection.get_rows_source(self)
        last_queryset = rows_source._get_first(reverse=True)
        if rows_source is self:
            self._continue_call_signature(cast("Self", last_queryset), ("last",))
        return cast("QuerySetSingle[TRow | None]", last_queryset)

    @overload
    def get(
        self,
        *args: Q,
        does_not_exist_exception: None,
        multiple_objects_returned_exception: GetExceptionArgument = GetException.STANDARD,
        **kwargs: Any,
    ) -> QuerySetSingle[TRow | None]: ...

    @overload
    def get(
        self,
        *args: Q,
        does_not_exist_exception: type[BaseException] | BaseException | GetException = GetException.STANDARD,
        multiple_objects_returned_exception: GetExceptionArgument = GetException.STANDARD,
        **kwargs: Any,
    ) -> QuerySetSingle[TRow]: ...

    @CallsBeforeSetup.recorded_result
    def get(
        self,
        *args: Q,
        does_not_exist_exception: GetExceptionArgument = GetException.STANDARD,
        multiple_objects_returned_exception: GetExceptionArgument = GetException.STANDARD,
        **kwargs: Any,
    ) -> QuerySetSingle[TRow] | QuerySetSingle[TRow | None]:
        """
        Fetches the one object matching the conditions.

        Args:
            args: ``Q`` conditions, as ``filter()`` takes them.
            does_not_exist_exception: What happens when no object matches: ``DoesNotExist`` is
                raised by default; an exception class (instantiated with no arguments) or instance
                is raised instead; None gives None.
            multiple_objects_returned_exception: What happens when more than one object matches:
                ``MultipleObjectsReturned`` is raised by default; an exception class or instance is
                raised instead; None reads one of them - which one is not defined - without
                counting the rest (``first()`` takes a defined one).
            kwargs: Field conditions, as ``filter()`` takes them.

        Raises:
            TypeError: An exception parameter is none of the above.
            QueryError: Conditions are given for a sliced queryset.
        """
        if does_not_exist_exception is not GetException.STANDARD:
            GetExceptions.check_exception_argument("does_not_exist_exception", does_not_exist_exception)
        if multiple_objects_returned_exception is not GetException.STANDARD:
            GetExceptions.check_exception_argument(
                "multiple_objects_returned_exception", multiple_objects_returned_exception
            )
        if self._combination is not None:
            return cast(
                "QuerySetSingle[TRow]",
                QuerySetCombination.get_combined_query(self)._get_single_queryset(
                    args,
                    kwargs,
                    does_not_exist_exception=does_not_exist_exception,
                    multiple_objects_returned_exception=multiple_objects_returned_exception,
                ),
            )
        if self._selection is not None:
            SingleRow.raise_if_sliced_values_rows_differ(self, "get")
        # The single-row queryset - typed without cast()'s call on the path of every get().
        return self._get_single_queryset(  # type: ignore[return-value]
            args,
            kwargs,
            does_not_exist_exception=does_not_exist_exception,
            multiple_objects_returned_exception=multiple_objects_returned_exception,
        )

    def insert_from(self, queryset: QuerySet[Any, Any], *, fields: Sequence[str]) -> InsertFromQuery:
        """Writes the rows ``queryset`` selects into the model's table in one ``INSERT ... SELECT`` -
        never read into Python; awaits to the number of rows written. The fields not named get their
        database default; an ``auto_now``/``auto_now_add`` field the moment of the insert,
        ``Meta.tenant_field`` the active tenant.

        Example:
            ::

                await Archive.objects.insert_from(
                    Event.objects.filter(finished=True).values("name", "tournament_id"),
                    fields=["title", "tournament"],
                )

        Args:
            queryset: A ``values()``/``values_list()`` queryset selecting one column per field, in
                order, or a queryset of models selecting its own fields of these names.
            fields: The written fields - concrete fields or forward relations to a single-column key.

        Returns:
            The insert.

        Raises:
            QueryError: See ``InsertFromQuery``; the queryset is combined.
            FieldError: See ``InsertFromQuery``.
        """
        QuerySetCombination.raise_if_combined(self, "insert_from")
        return InsertFromQuery(self, queryset, fields)

    async def refresh_materialized_view(self, name: str, *, concurrently: bool = False) -> None:
        """Fills a materialized view of ``Meta.materialized_views`` with the rows of its query again,
        on the connection the model writes to.

        Example:
            ::

                await Order.objects.refresh_materialized_view("order_totals", concurrently=True)

        Args:
            name: The view's name.
            concurrently: Keep the view readable while it refreshes - needs its ``unique_columns``.

        Raises:
            QueryError: The model declares no such view, or ``concurrently`` isn't a bool.
            UnSupportedError: The database has no materialized views.
            ConfigurationError: ``concurrently`` for a view without ``unique_columns``.
        """
        if not isinstance(concurrently, bool):
            raise QueryError(f"refresh_materialized_view(concurrently=...) takes a bool, got {concurrently!r}")
        view = SchemaObjectCalls.get_declared_schema_object(
            self, self.model._meta.materialized_views, name, "materialized view"
        )
        editor = SchemaObjectCalls.get_schema_editor(self)
        editor.raise_if_unsupported("supports_materialized_views", "materialized views")
        await editor.materialized_views.refresh_materialized_view(self.model, view, concurrently)

    async def reload_dictionary(self, name: str) -> None:
        """Loads a dictionary of ``Meta.dictionaries`` from its source again, on the connection the
        model writes to - the rows written since its last load are read through it from then on.

        Example:
            ::

                await Country.objects.reload_dictionary("country_names")

        Args:
            name: The dictionary's name.

        Raises:
            QueryError: The model declares no such dictionary.
            UnSupportedError: The database has no dictionaries.
        """
        dictionary = SchemaObjectCalls.get_declared_schema_object(
            self, self.model._meta.dictionaries, name, "dictionary"
        )
        editor = SchemaObjectCalls.get_schema_editor(self)
        editor.raise_if_unsupported("supports_dictionaries", "dictionaries")
        await editor.dictionaries.reload_dictionary(self.model, dictionary)

    async def get_next_sequence_value(self, name: str) -> int:
        """Takes the next number of a sequence of ``Meta.sequences``, on the connection the model
        writes to - a number taken is never handed out again, even when the transaction rolls back.

        Example:
            ::

                number = await Invoice.objects.get_next_sequence_value("invoice_number")

        Args:
            name: The sequence's name.

        Returns:
            The number.

        Raises:
            QueryError: The model declares no such sequence.
            UnSupportedError: The database has no sequences.
        """
        sequence = SchemaObjectCalls.get_declared_schema_object(self, self.model._meta.sequences, name, "sequence")
        editor = SchemaObjectCalls.get_schema_editor(self)
        editor.raise_if_unsupported("supports_sequences", "sequences")
        return await editor.sequences.get_next_sequence_value(self.model, sequence)

    def merge(self, source: Any, *, on: str | Sequence[str] | Mapping[str, str]) -> MergeQuery:
        """One ``MERGE`` statement matching ``source``'s rows to the model's rows (of this queryset) on
        ``on`` and, branch by branch, updating, deleting or inserting them - the branches are added
        with ``when_matched()``, ``when_not_matched()`` and ``when_not_matched_by_source()``.

        Example:
            ::

                await (
                    Stock.objects.merge(deliveries.values("sku", "delivered"), on="sku")
                    .when_matched(update={"count": F("count") + MergeSource("delivered")})
                    .when_not_matched(insert={"sku": MergeSource("sku"), "count": MergeSource("delivered")})
                )

        Args:
            source: A ``values()`` queryset, a queryset of models, a union of ``values()`` querysets,
                or a list of dicts keyed by field names of the model.
            on: The matched fields - names the source has columns of too, or a dict of the model's
                field to the source column.

        Returns:
            The merge.

        Raises:
            QueryError: See ``MergeQuery``; the queryset is combined.
            FieldError: See ``MergeQuery``.
        """
        QuerySetCombination.raise_if_combined(self, "merge")
        return MergeQuery(self, source, on)

    def bulk_create(
        self,
        objects: Iterable[TModel],
        batch_size: int | None = None,
        *,
        ignore_conflicts: bool = False,
        update_fields: Iterable[str] | None = None,
        on_conflict: Iterable[str] | None = None,
        on_conflict_constraint: str | None = None,
        conflict_where: Q | RawSQLTerm | None = None,
        returning: bool | None = None,
        use_copy: bool = False,
    ) -> BulkCreateQuery[TModel]:
        """Inserts the objects efficiently - generally one query. A ``db_default`` field must be set on
        all objects or on none.

        Args:
            objects: The objects to create.
            batch_size: How many objects one query creates.
            ignore_conflicts: Skip the rows conflicting with an existing one.
            update_fields: The fields a conflicting row takes from the object (upsert) - a relation
                name means its key column(s). For a tenant-scoped model only a conflicting row of
                the active tenant is updated. A conflicting row also gets its ``auto_now`` fields
                and ``Meta.optimistic_lock_field`` bumped.
            on_conflict: The conflict target's field names.
            on_conflict_constraint: The conflict target as a named constraint - instead of
                ``on_conflict``. Not on SQLite.
            conflict_where: The ``WHERE`` of the partial unique index the target matches - a ``Q``
                over the model's fields or ``RawSQLTerm`` of raw SQL. Needs ``on_conflict``. Not on
                SQLite.
            returning: Read the database-generated primary key, generated columns and omitted
                ``db_default`` columns back onto each object. Not on SQLite. With
                ``ignore_conflicts`` it needs ``on_conflict=[...]`` to match the rows. None takes
                ``Meta.returning``. Not with ``use_copy``.
            use_copy: Load the rows through the database's bulk load of rows
                (``Features.supports_copy``) - faster for large batches, with no ``RETURNING`` and
                no conflict handling. A database whose usual way to write many rows it is
                (``Features.copies_bulk_inserts``) takes it with or without this.

        Raises:
            ValueError: The parameters contradict each other.
            UnSupportedError: The dialect has no such option, or ``use_copy`` is combined with
                ``returning`` or conflict handling.
            QueryError: A tenant-scoped model has no active tenant or an object of another one;
                ``returning=True`` with skipped rows has no ``on_conflict``; ``update_fields`` names
                a generated, tenant, lock or soft-delete field; ``batch_size`` isn't positive; an
                object isn't of this model; a ``db_default`` field is set on some objects only.
            IncompleteInstanceError: A ``.only()``/``.defer()`` object lacks a written field.
            FieldError: ``update_fields``/``on_conflict`` names something that isn't a column field
                of the model.
        """
        return self._share_ambient_scope(
            BulkCreateQuery(
                connection=self._connection,
                model=self.model,
                objects=objects,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_fields=update_fields,
                on_conflict=on_conflict,
                on_conflict_constraint=on_conflict_constraint,
                conflict_where=conflict_where,
                returning=returning,
                use_copy=use_copy,
                all_tenants=self._visibility.all_tenants,
            )
        )

    def bulk_update(
        self,
        objects: Iterable[TModel],
        fields: Iterable[str],
        batch_size: int | None = None,
        *,
        returning: bool | None = None,
    ) -> BulkUpdateQuery[TModel]:
        """
        Update the given fields in each of the given objects in the database.

        Args:
            objects: List of objects to bulk update
            fields: The fields to update - a list of field names; a repeated name is written once
            batch_size: How many objects are updated in a single query. Without it, the objects are
                split only to stay under the bind-parameter ceiling, and those statements run in
                one transaction - an error in any leaves no row updated (a stale version doesn't
                roll back the others). With it, every batch is an independent statement.
            returning: When True, adds a ``RETURNING`` clause bringing back
                every ``GeneratedField`` column's fresh, DB-recomputed value (a column depending on
                one of the updated ``fields`` may have changed as a result) and applies it onto each
                successfully-updated object - mirrors ``bulk_create(returning=True)``'s own
                RETURNING support. A no-op when the model has no ``GeneratedField`` at all. Matched
                back to each object by its own primary key, not by row position (unlike
                ``bulk_create()``'s multi-row ``INSERT``, ``UPDATE ... FROM (VALUES ...)`` gives no
                row-order guarantee), so this works on every dialect that supports ``RETURNING``, not
                just Postgres. Left as ``None`` (the default), this falls back to ``Meta.returning``
                (itself defaulting to ``False``, see ``bulk_create()``'s own docstring for the same
                mechanism) - an explicit ``True``/``False`` here always overrides it.

        Raises:
            ValueError: If objects have no primary key set
            QueryError: If ``fields`` is a string, empty or names the primary key, or an object
                isn't an instance of this queryset's model.
            FieldError: If ``fields`` names something that isn't a field of the model.
            IncompleteInstanceError: If a ``.only()``/``.defer()`` object lacks its pk or an
                updated field.
            QueryError: If ``Meta.soft_delete_field`` is among ``fields`` - use
                ``.delete()`` (which applies cascade logic a direct field update would bypass) or
                ``.restore()`` (``cascade=True`` brings back what the delete cascaded) instead; if
                ``Meta.optimistic_lock_field`` is among
                ``fields`` - it's bumped automatically, not set directly; if a ``GeneratedField``
                is among ``fields`` - it's computed by the database, not written to; if
                ``Meta.tenant_field`` is set, this queryset wasn't built via ``.all_tenants()``,
                and either no tenant is active, one
                of ``objects`` belongs to a tenant other than the active one, or one of
                ``objects`` still has an unresolved async ``default=`` value pending for
                ``tenant_field`` (save or refresh it first); if two or more objects in
                ``objects`` share the same primary key; or if a field being updated holds an
                ``F()``/expression value (use ``QuerySet.update()`` or ``save()`` for those).
        """
        return BulkUpdateQuery(self, objects, fields, batch_size=batch_size, returning=returning)

    async def create(self, **kwargs: Any) -> TModel:
        """
        Creates a row and returns its object - ``Model(**kwargs)`` saved on the queryset's
        connection. The queryset's filters don't take part.

        Args:
            kwargs: The field values.

        Raises:
            QueryError: A value is an ``F()``/``Expression`` - there is no row yet to read a
                current value from; or the model has ``Meta.tenant_field`` and the tenant given
                differs from the active one, or none is given and none is active.
        """
        ValuesArguments.raise_if_values_selected(self, "create")
        QuerySetCombination.raise_if_combined(self, "create")
        model = self.model
        for field_name, value in kwargs.items():
            if isinstance(value, Expression):
                raise QueryError(
                    f"create() on {model.__name__} received {field_name}={value!r} - F()/Expression "
                    "values aren't supported when creating a new row, since there's no existing "
                    "row to read a current value from. Pass a plain value instead."
                )
        if model._meta.tenant_field:
            Tenancy.fill_create_values(model, kwargs)
        # Model.__init__ leaves the instance unsaved.
        instance = model(**kwargs)
        await instance.save(using=self.get_connection(for_write=True), force_create=True)
        return instance

    async def get_or_create(self, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[TModel, bool]:
        """
        Fetches the object matching ``kwargs`` among the queryset's rows, or creates it.

        Args:
            defaults: Values of a created object on top of ``kwargs`` (its lookups left out).
            kwargs: The conditions, as ``get()`` takes them.

        Returns:
            The object and whether it was created.

        Raises:
            QueryError: ``defaults`` conflicts with ``kwargs``, or per ``create()``.
            IntegrityError: The create failed for a reason other than the row already existing.
        """
        ValuesArguments.raise_if_values_selected(self, "get_or_create")
        QuerySetCombination.raise_if_combined(self, "get_or_create")
        # The existence check reads on the connection the row would be created on: a replica may not
        # have the row yet.
        queryset = CreateOrUpdate.pinned_for_write(self)
        try:
            return await CreateOrUpdate.get_matching(queryset, kwargs), False
        except DoesNotExist:
            return await CreateOrUpdate.create_or_get(queryset, defaults or {}, kwargs)

    async def update_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        create_defaults: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[TModel, bool]:
        """
        Updates the object matching ``kwargs`` among the queryset's rows with ``defaults``, or
        creates it.

        Args:
            defaults: The values to update the object with - and to create it with, when
                ``create_defaults`` isn't given.
            create_defaults: Values of a created object instead of ``defaults``.
            kwargs: The conditions, as ``get()`` takes them.

        Returns:
            The object and whether it was created.

        Raises:
            QueryError: ``defaults`` sets ``Meta.optimistic_lock_field``, or per ``create()`` for
                a created object.
            FieldError: ``defaults`` names a field the model doesn't have.
        """
        ValuesArguments.raise_if_values_selected(self, "update_or_create")
        QuerySetCombination.raise_if_combined(self, "update_or_create")
        defaults = defaults or {}
        # Pinned to the write connection: the SELECT FOR UPDATE and the UPDATE after it must
        # land on one connection for the lock to mean anything.
        queryset = CreateOrUpdate.pinned_for_write(self)
        async with queryset._connection._in_transaction() as connection:
            matching = queryset.using(connection).filter(**kwargs)
            if connection.features.supports_select_for_update:
                matching = matching.select_for_update()
            instance = cast("TModel | None", await matching.get(does_not_exist_exception=None))
            if instance is not None:
                # The lock field is refused only here, for an existing row - a created row may
                # set its initial version.
                await CreateOrUpdate.update_with_defaults(queryset, instance, defaults, connection)
                return instance, False
        instance, created = await CreateOrUpdate.create_or_get(
            queryset, defaults if create_defaults is None else create_defaults, kwargs
        )
        if not created and defaults:
            # A concurrent writer created the row first - this call's values still apply to it.
            await CreateOrUpdate.update_with_defaults(queryset, instance, defaults, queryset._connection)
        return instance, created

    @CallsBeforeSetup.recorded
    def only(self, *fields_for_select: str) -> Self:
        """Fetches only the given fields, creating partial model instances. Reading a field left out
        raises ``AttributeError``; ``save()`` of a partial instance needs ``update_fields`` naming
        loaded fields and the primary key loaded, else it raises ``IncompleteInstanceError``.

        Raises:
            ValueError: No field names are given, or ``.defer()`` was already used.
        """
        QuerySetCombination.raise_if_combined(self, "only")
        ValuesArguments.raise_if_values_selected(self, "only")
        FilterArguments.check_selected_names(self, fields_for_select, "only")
        if not fields_for_select:
            raise QueryError(".only() requires at least one field")
        if self._deferred_fields:
            raise QueryError(".only() cannot be combined with .defer() on the same queryset")
        queryset = self._clone()
        # "pk" and a forward relation name load their own key field(s), like Django - a to-many
        # relation name is left as given.
        only_field_names: list[str] = []
        for field_name in fields_for_select:
            concrete_field_paths = ConcreteFieldPaths.get_paths(self.model, field_name)
            if any(path.startswith(f"{field_name}__") for path in concrete_field_paths):
                concrete_field_paths = (field_name,)
            only_field_names.extend(concrete_field_paths)
        queryset._fields_for_select = tuple(dict.fromkeys(only_field_names))
        return self._continue_call_signature(queryset, ("only", fields_for_select))

    @CallsBeforeSetup.recorded
    def defer(self, *fields: str) -> Self:
        """Fetches every direct field of the model except the given ones, creating partial model
        instances. Only direct fields can be deferred; ``.select_related()`` relations are still
        loaded in full.

        Raises:
            ValueError: No field names are given, or ``.only()`` was already used.
            FieldError: A name is not a direct field of the model.
        """
        QuerySetCombination.raise_if_combined(self, "defer")
        ValuesArguments.raise_if_values_selected(self, "defer")
        if not fields:
            raise QueryError(".defer() requires at least one field")
        if self._fields_for_select:
            raise QueryError(".defer() cannot be combined with .only() on the same queryset")
        direct_fields = self.model._meta.fields_map.keys() - self.model._meta.fetch_fields
        unknown_fields = set(fields) - direct_fields
        if unknown_fields:
            raise FieldError(f"Unknown direct field(s) for .defer(): {sorted(unknown_fields)}")
        queryset = self._clone()
        queryset._deferred_fields = tuple(dict.fromkeys((*self._deferred_fields, *fields)))
        return queryset

    @CallsBeforeSetup.recorded
    def using(self, using: str | DatabaseClient | None) -> Self:
        """Runs the query on the given connection.

        Args:
            using: A connection's name, or a connection's client - a transaction's included;
                None keeps the connection the query would choose itself.
        """
        connection_alias = Connections.get(using) if isinstance(using, str) else using
        queryset = self._clone_keeping_calls()
        queryset._apply_connection(connection_alias or queryset._connection)
        if connection_alias:
            queryset._connection_explicitly_chosen = True
        # The connection is in the key of the plan a queryset's calls run on - it shapes no call.
        return self._continue_call_signature(queryset, None)

    def _set_instance_connection(self, obj: Model) -> None:
        """Makes this queryset fall back to the connection ``obj`` was loaded from or saved to
        when the router has no opinion. The queried model keeps its own choice when the obj
        never touched the database, when its default connection differs from the obj model's,
        or when the router routes the obj's model.

        Args:
            obj: The model obj the relation is read from.
        """
        if (
            obj._connection_alias is not None
            and self.model._meta.default_connection == type(obj)._meta.default_connection
            and not InstanceConnections.is_routed(type(obj))
        ):
            self._instance_connection_alias = obj._connection_alias

    def _share_ambient_scope(self, query: TDerivedQuery) -> TDerivedQuery:
        """Hands this queryset's default scope (``.all_tenants()``/``.include_deleted()`` state
        included) to a derived (count/update/delete/...) query, together with the connection of
        the model instance a related manager built it from.

        Args:
            query: The query just built from this queryset.

        Returns:
            ``query`` itself, with the ambient scope set.
        """
        query._instance_connection_alias = self._instance_connection_alias
        return self._share_default_scope(query)

    def __await__(self) -> Generator[Any, None, list[TRow]]:
        connection: DatabaseClient | None = self._connection
        # A joined relation's rows are read by the query's own layout.
        if self._call_signature is not None and not self._select_related:
            # The connection is chosen once - the query run as usual takes it.
            connection = connection or self.get_connection()
            if self._selection is not None:
                awaitable = CallSignatureRuns.await_call_signature_values(self, connection)
            else:
                # Annotations are read by the query's own layout.
                awaitable = (
                    None if self._annotations else CallSignatureRuns.await_call_signature_rows(self, connection)
                )
            if awaitable is not None:
                return awaitable
        query = StatementSelection.get_select_query(self)
        if connection is not None and cast("DatabaseClient | None", query._connection) is None:
            query._apply_connection(connection)
        if self._combination is None:
            self._hand_call_signature(query)
        # Made for this run alone - built and run as it is, not copied first.
        query._is_execution_query = True
        return query.__await__()

    async def __aiter__(self) -> AsyncIterator[TRow]:
        for row in await self:
            yield row

    def iterator(self, chunk_size: int = 1000) -> AsyncIterator[TRow]:
        """Pages through the result ``chunk_size`` rows at a time - one page in memory at a time, with
        no server-side cursor.

        Without an ``order_by()``, pages follow ``Meta.ordering``, else the primary key. An ordering
        that can tie gets the primary key appended as a tie-breaker. The queryset's own slice and
        ``.after_cursor()`` bound the whole iteration.

        When every ordering field is a column of this model, pages are fetched by keyset - a row
        deleted from a page already yielded never makes a later page skip a row. An ordering across
        a relation or by an annotation, a primary key that can repeat, or a window function falls
        back to ``OFFSET`` paging, which is not safe under concurrent deletes.

        Args:
            chunk_size: The page size.

        Returns:
            The rows, as an async iterator. Iterating raises QueryError: ``chunk_size`` isn't a
            positive integer, or a ``before_cursor()`` query.
        """
        return RowsQuery.iterate_query(partial(StatementSelection.get_select_query, self), chunk_size)

    def stream(self, chunk_size: int = 1000) -> AsyncIterator[TRow]:
        """Streams the rows off one server-side cursor (PostgreSQL) - a single consistent snapshot for
        the whole scan, unlike ``iterator()``, which runs a SELECT per page. Requires an open
        ``Transactions.atomic()`` block. Iterating raises:

        - QueryError: ``chunk_size`` isn't a positive integer, the call is outside a transaction,
          a ``before_cursor()`` query, or model instances with ``prefetch_related()``.
        - UnSupportedError: The database has no streaming (``features.supports_streaming``).

        Args:
            chunk_size: How many rows to fetch per round trip.

        Returns:
            The rows, as an async iterator.
        """
        return RowsQuery.stream_query(partial(StatementSelection.get_select_query, self), chunk_size)

    @CallsBeforeSetup.recorded
    def select_related(self, *args: str | Select) -> Self:
        """Returns a queryset that also selects the given forward relations with a JOIN. Pass
        ``Select(relation, extra_condition=Q(...))`` instead of a name to condition that relation's
        JOIN itself.
        """
        QuerySetCombination.raise_if_combined(self, "select_related")
        ValuesArguments.raise_if_values_selected(self, "select_related")
        args = GenericForeignKeyPaths.expand_relation_paths(self.model, args, "select_related")

        queryset = self._clone()
        for arg in args:
            self._check_select_related_path(arg.relation if isinstance(arg, Select) else arg)
            if isinstance(arg, Select):
                queryset._select_related.add(arg.relation)
                queryset._explicitly_select_related = queryset._explicitly_select_related | {arg.relation}
                if arg.extra_condition is not None:
                    queryset._select_related_extra_conditions = {
                        **queryset._select_related_extra_conditions,
                        arg.relation: arg.extra_condition,
                    }
            else:
                queryset._select_related.add(arg)
                queryset._explicitly_select_related = queryset._explicitly_select_related | {arg}
        if all(type(arg) is str for arg in args):
            return self._continue_call_signature(queryset, ("select_related", args))
        return queryset

    def _check_select_related_path(self, relation_path: str) -> None:
        """Checks every hop of a ``select_related()`` path is a single-valued relation.

        Raises:
            FieldError: A hop is not found, is not a relation, or is a reverse ForeignKey /
                ManyToManyField (joining one would repeat the row once per related row).
        """
        lookup_path = LookupPath.parse(self.model, relation_path, crosses_last=True)
        model: type[Model] = self.model
        for part, related_field in zip(lookup_path.relation_names, lookup_path.relations, strict=True):
            if related_field.is_multi_valued:
                raise FieldError(
                    f"select_related() can't follow {part!r} on {model._meta.full_name} - it can hold many "
                    "related rows (a reverse ForeignKey or a ManyToManyField); use prefetch_related() instead"
                )
            model = related_field.related_model
        if lookup_path.rest:
            part = lookup_path.rest[0]
            if part in model._meta.fields_map:
                raise FieldError(f"select_related() field {part!r} on {model._meta.full_name} is not a relation")
            raise FieldError(f"select_related() relation {part!r} for {model._meta.full_name} not found")

    @CallsBeforeSetup.recorded
    def prefetch_related(self, *args: str | Prefetch) -> Self:
        """
        Loads the given relations of every row with one query per relation - the rows of
        the queryset and, through ``prefetch_related_objects()``, instances already in hand.

        Raises:
            FieldError: If the field to prefetch on is not a relation, or not found.
        """
        args = GenericForeignKeyPaths.expand_relation_paths(self.model, args, "prefetch_related")
        if self._combination is not None:
            return cast(
                "Self",
                CombinedDerivedQueries.get_prefetching_queryset(QuerySetCombination.get_combined_query(self), args),
            )
        ValuesArguments.raise_if_values_selected(self, "prefetch_related")
        queryset = self._clone_keeping_calls()
        Prefetch.add_lookups(self.model, queryset._prefetch_map, queryset._prefetch_queries, args)
        # The prefetched relations - a forward relation's key column is selected for its prefetch
        # even past only().
        return self._continue_call_signature(
            queryset,
            ("prefetch_related", tuple(sorted({*queryset._prefetch_map, *queryset._prefetch_queries}))),
        )

    @CallsBeforeSetup.recorded
    def defer_related(self, *fields: str) -> Self:
        """Opts the given relations out of their field-level ``lazy="joined"``/``"select"`` default for
        this query.
        """
        QuerySetCombination.raise_if_combined(self, "defer_related")
        ValuesArguments.raise_if_values_selected(self, "defer_related")
        queryset = self._clone()
        queryset._deferred_related_fields = queryset._deferred_related_fields | set(fields)
        return queryset


# Imported last: the statement modules import this one.
from hare.query.statements.select.raw_sql_query import RawSQLQuery  # noqa: E402
from hare.query.statements.summary.aggregate_query import AggregateQuery  # noqa: E402
from hare.query.statements.summary.contains_query import ContainsQuery  # noqa: E402
from hare.query.statements.summary.count_query import CountQuery  # noqa: E402
from hare.query.statements.summary.exists_query import ExistsQuery  # noqa: E402
from hare.query.statements.write.bulk.bulk_update_query import BulkUpdateQuery  # noqa: E402
from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery  # noqa: E402
from hare.query.statements.write.declarations import HardDeleteQuery  # noqa: E402
from hare.query.statements.write.delete_query import DeleteQuery  # noqa: E402
from hare.query.statements.write.insert_from_query import InsertFromQuery  # noqa: E402
from hare.query.statements.write.merge.merge_query import MergeQuery  # noqa: E402
from hare.query.statements.write.restore_query import RestoreQuery  # noqa: E402
from hare.query.statements.write.update_query import UpdateQuery  # noqa: E402
