from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Generator, Iterable, Sequence
from dataclasses import replace
from itertools import repeat
from operator import is_
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, Self, TypeVar, cast, overload

from hare.core.connections import Connections
from hare.core.lookup_path import LookupPath
from hare.core.model_cache import ModelCache
from hare.dialects.base.client.database_client import DatabaseClient, retryable_read_query_active
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.exceptions import (
    DoesNotExist,
    FieldError,
    IncompleteInstanceError,
    IntegrityError,
    MultipleObjectsReturned,
    QueryError,
    UnSupportedError,
)
from hare.fields.base.database_default import DatabaseDefault
from hare.fields.enums import RelationLoadStrategy
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.backward_one_to_one_relation import BackwardOneToOneRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models.tenancy import Tenancy
from hare.query.constants import (
    GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK,
    SELECT_FOR_UPDATE_OPTION_NAMES,
)
from hare.query.enums import RowShape
from hare.query.expressions import Exists, Expression, F, Ordering, Q, Subquery
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.filters import FieldLookups
from hare.query.filters.constants import DATETIME_CAST_SEGMENTS
from hare.query.functions.datetime.extract import Extract
from hare.query.functions.datetime.trunc import Trunc
from hare.query.lookup_info.lookup_info import LookupInfo
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.query.lookup_info.ordering_info import OrderingInfo
from hare.query.lookup_paths import LookupPaths
from hare.query.plans.call_signature_plans import CallSignaturePlans
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.statement_plans import StatementPlans
from hare.query.queryset.calls_before_setup import CallsBeforeSetup
from hare.query.queryset.combination import Combination
from hare.query.queryset.direct_get import DirectGet
from hare.query.queryset.none_result import NoneAwaitable
from hare.query.queryset.query_options import QueryOptions
from hare.query.queryset.query_spec import QuerySpec
from hare.query.queryset.single_result import QuerySetSingle
from hare.query.queryset.values_selection import ValuesSelection
from hare.query.relation_loading.prefetch import Prefetch
from hare.query.relation_loading.select import Select
from hare.query.rows.hydrate_accelerator import HydrateAccelerator
from hare.query.rows.model_rows import ModelRows
from hare.query.scopes.row_scopes import RowScopes
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from hare.query.statements.select.rows_query import RowsQuery
from hare.sql import Order
from hare.sql.enums import SetOperation
from hare.sql.queries.tables.selectable import Selectable
from hare.sql.terms.base.term import Term
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.base.field import Field
    from hare.models import Model
    from hare.query.plans.statement_plan import StatementPlan
    from hare.query.statements.select.combined_query import CombinedQuery
    from hare.query.statements.select.raw_sql_query import RawSQLQuery
    from hare.query.statements.select.values_query import ValuesQuery
    from hare.query.statements.summary.aggregate_query import AggregateQuery
    from hare.query.statements.summary.contains_query import ContainsQuery
    from hare.query.statements.summary.count_query import CountQuery
    from hare.query.statements.summary.exists_query import ExistsQuery
    from hare.query.statements.write.bulk_create_query import BulkCreateQuery
    from hare.query.statements.write.bulk_update_query import BulkUpdateQuery
    from hare.query.statements.write.delete_query import DeleteQuery, HardDeleteQuery
    from hare.query.statements.write.update_query import UpdateQuery

TModel = TypeVar("TModel", bound="Model")
#: What a row of a queryset is - the model instance, or what ``.values()``/``.values_list()`` select.
TRow = TypeVar("TRow", default=TModel)
TDerivedQuery = TypeVar("TDerivedQuery", bound="AwaitableQuery[Any]")
TMadeQuerySet = TypeVar("TMadeQuerySet", bound="QuerySet[Any, Any]")


class QuerySet(QuerySpec[TModel], Generic[TModel, TRow]):
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
    )

    # The dict/list/set slots a clone gets its own shallow copy of. `_prefetch_queries` is a dict of
    # lists - _clone() copies it separately.
    mutable_clone_slots: ClassVar[dict[str, str]] = QuerySpec.MUTABLE_SPEC_SLOTS

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
        self._raise_if_combined("all_tenants")
        queryset = self._clone()
        queryset._visibility = replace(queryset._visibility, all_tenants=True)
        return queryset

    @CallsBeforeSetup.recorded
    def include_deleted(self) -> Self:
        """Returns a clone including soft-deleted rows. After ``.only_deleted()`` it replaces it.

        Raises:
            QueryError: The model has no ``Meta.soft_delete_field``.
        """
        self._raise_if_combined("include_deleted")
        return self._with_deleted_rows(only_deleted=False)

    @CallsBeforeSetup.recorded
    def only_deleted(self) -> Self:
        """Returns a clone with only soft-deleted rows; relations reached from it see deleted rows too.
        The ``Meta.tenant_field`` filter still applies. After ``.include_deleted()`` it replaces it.

        Raises:
            QueryError: The model has no ``Meta.soft_delete_field``.
        """
        self._raise_if_combined("only_deleted")
        return self._with_deleted_rows(only_deleted=True)

    def _with_deleted_rows(self, *, only_deleted: bool) -> Self:
        """A clone seeing soft-deleted rows - them alone, or together with the live ones.

        Args:
            only_deleted: See only the soft-deleted rows of the model itself.

        Raises:
            QueryError: If the model has no ``Meta.soft_delete_field`` configured.
        """
        self._raise_if_combined("only_deleted" if only_deleted else "include_deleted")
        if not self.model._meta.soft_delete_field:
            raise QueryError(f"{self.model.__name__} has no Meta.soft_delete_field configured")
        queryset = self._clone()
        queryset._visibility = replace(queryset._visibility, include_deleted=True, only_deleted=only_deleted)
        return queryset

    def _clone(self) -> Self:
        queryset = self._clone_keeping_calls()
        queryset._call_signature = None
        return queryset

    def _takes_pending_filters(self, kwargs: dict[str, Any]) -> bool:
        """Whether filter kwargs can be kept unbuilt - their conditions are plain ``Q`` objects, not
        ones built and checked over several key fields (a composite primary key, ``relation__pk``,
        a many-to-many relation) right when ``.filter()`` is called.

        Args:
            kwargs: The filter kwargs.

        Returns:
            True when they can.
        """
        meta = self.model._meta
        if meta.has_composite_primary_key:
            return False
        m2m_fields = meta.m2m_fields
        for key in kwargs:
            # A relation's own key compared through its key fields: __pk, __pk__in.
            if key in m2m_fields or key[-4:] == "__pk" or key[-8:] == "__pk__in":
                return False
        return True

    def _clone_keeping_calls(self) -> Self:
        """A clone keeping this queryset's call signature - for a simple call to extend it.

        Returns:
            The clone.
        """
        # _prefetch_queries is a dict of lists - each list is copied, so clones don't share them.
        if self._direct_get is not None:
            self._apply_direct_get_filters()
        queryset: Self = QuerySpec.__copy__(self)  # type: ignore[assignment]
        prefetch_queries = self._prefetch_queries
        queryset._prefetch_queries = (
            {key: list(value) for key, value in prefetch_queries.items()} if prefetch_queries else {}
        )
        return queryset

    @staticmethod
    def _is_subquery_filter_value(value: Any) -> bool:
        """Whether a filter value is a query used as a subquery rather than bound values.

        Args:
            value: The filter value.

        Returns:
            True for a queryset, values query, union or ``Subquery``.
        """
        return isinstance(value, (QuerySpec, Subquery))

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
        conditions.extend(self._get_filter_kwarg_conditions(kwargs, generation))
        self._add_filter_conditions(negate, conditions, generation)

    def _raise_if_slice_taken(self, message: str) -> None:
        """Raises when the rows are a slice - changing the filters, the ordering or the distinct rows
        would change which rows the slice holds. A single-row queryset not taken from a slice is
        narrowed only when fetched.

        Args:
            message: The error message.

        Raises:
            QueryError: The queryset is sliced.
        """
        if (self._limit is not None or self._offset) and (not self._single or self._is_single_row_of_slice):
            raise QueryError(message)

    def _filter_or_exclude(self, negate: bool, args: tuple[Q | Exists, ...], kwargs: dict[str, Any]) -> Self:
        self._raise_if_slice_taken("Cannot filter a query once a slice has been taken.")
        self._check_filter_keys(args, kwargs, "exclude" if negate else "filter")
        # An iterable __in value is read once - the filter and the call signature share the list.
        kwargs = {key: Q.get_list_lookup_value(key, value) for key, value in kwargs.items()}
        if (
            not args
            and self._call_signature is not None
            and self._direct_get is None
            and self._takes_pending_filters(kwargs)
        ):
            for value in kwargs.values():
                if isinstance(value, (Plannable, Term, QuerySpec, Subquery)):
                    break
            else:
                # Built only when a query of the queryset doesn't run on the plan of its calls.
                queryset = self._clone_keeping_calls()
                queryset._filter_call_counter += 1
                queryset._pending_filter_calls = (
                    *self._pending_filter_calls,
                    (negate, queryset._filter_call_counter, kwargs),
                )
                return self._continue_call_signature(
                    queryset, ("exclude" if negate else "filter", tuple(kwargs)), tuple(kwargs.values())
                )
        queryset = self._clone()
        queryset._append_filters(negate, args, kwargs)
        return queryset

    def _check_filter_keys(self, conditions: tuple[Q | Exists, ...], kwargs: dict[str, Any], method_name: str) -> None:
        """Checks every filter key right away, through ``get_lookup_info()`` - the whole path
        through relations, the field and the lookup, or an annotation and its lookup.

        Args:
            conditions: The ``Q``/``Exists`` conditions.
            kwargs: The keyword filters.
            method_name: ``filter`` or ``exclude``, named in the error.

        Raises:
            FieldError: A key names no field, relation or annotation, or a lookup its field doesn't
                have.
            QueryError: A key is a lookup other than equality, membership or ``isnull`` on a
                relation to a composite key.
        """
        keys = list(kwargs)
        pending_q_objects = [condition for condition in conditions if isinstance(condition, Q)]
        while pending_q_objects:
            q_object = pending_q_objects.pop()
            keys.extend(q_object.filters)
            pending_q_objects += [child for child in q_object.children if isinstance(child, Q)]
        meta = self.model._meta
        model_name = self.model.__name__
        for key in keys:
            label = f"{model_name}.objects.{method_name}({key}=...)"
            name, __, rest = key.partition("__")
            if name in self._annotations:
                # A path inside an annotation's value is only valid for a JSON value - the
                # annotation's type is resolved to tell, only for such a path.
                needs_output_fields = bool(rest) and rest not in FieldLookups.get(None)
                self._get_annotation_description(
                    "checked_filter",
                    key,
                    lambda: LookupInfoBuilder.get_lookup_info(
                        self.model,
                        key,
                        annotations=self._annotations,
                        annotation_fields=self._get_annotation_output_fields() if needs_output_fields else None,
                        label=label,
                    ),
                )
            else:
                meta._get_lookup_info(key, label)

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
        return self._get_annotation_description(
            "lookup_info",
            key,
            lambda: LookupInfoBuilder.get_lookup_info(
                self.model,
                key,
                annotations=self._annotations,
                annotation_fields=self._get_annotation_output_fields(),
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
        lookups = self._get_annotation_description(
            f"lookups:{dialect.name}",
            path,
            lambda: LookupInfoBuilder.get_lookups(
                self.model,
                path,
                dialect,
                annotations=self._annotations,
                annotation_fields=self._get_annotation_output_fields(),
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
        return self._get_annotation_description(
            "ordering_info",
            name,
            lambda: LookupInfoBuilder.get_ordering_info(
                self.model, name, annotations=self._annotations, annotation_fields=self._get_annotation_output_fields()
            ),
        )

    def _get_annotation_output_fields(self) -> dict[str, Field[Any] | None]:
        """The field whose type each annotation's value has, resolved once per queryset and dialect -
        with the neutral SQL dialect until a connection is chosen.

        Returns:
            Each annotation name to its value's field, None where that isn't known.
        """
        descriptions = self._get_annotation_descriptions()
        dialect_name = self._get_analysis_dialect().name
        missing_names = [
            name for name in self._annotations if ("output_field", name, dialect_name) not in descriptions
        ]
        if missing_names:
            expression_context = self._get_probe_expression_context(self._annotations)
            for name in missing_names:
                annotation = self._annotations[name]
                descriptions[("output_field", name, dialect_name)] = (
                    annotation.get_value_field(annotation.get_result(expression_context))
                    if isinstance(annotation, Expression)
                    else None
                )
        return {name: descriptions[("output_field", name, dialect_name)] for name in self._annotations}

    def _get_annotation_descriptions(self) -> dict[tuple[str, str, str], Any]:
        """The queryset's cached descriptions of keys starting with an annotation - by (description type, key,
        dialect name), the dialect the annotations' types were resolved with. Emptied once a
        model's fields, lookups or relations change (``LookupInfoBuilder.forget_descriptions()``).

        Returns:
            The cache, shared with the clones that have the same annotations.
        """
        descriptions = self._annotation_descriptions
        if descriptions is None:
            descriptions = self._annotation_descriptions = (
                LookupInfoBuilder.annotation_descriptions.new_shared_bucket()
            )
        return descriptions

    def _get_annotation_description(self, description_type: str, key: str, build: Callable[[], Any]) -> Any:
        """A description of a key starting with one of the queryset's annotations, built once per
        queryset annotations and dialect - an error is raised again on every call, never cached.

        Args:
            description_type: What is described - ``lookup_info``, ``ordering_info``, ...
            key: The filter key, ordering name or path.
            build: Builds the description.

        Returns:
            The description.
        """
        descriptions = self._get_annotation_descriptions()
        cache_key = (description_type, key, self._get_analysis_dialect().name)
        description = descriptions.get(cache_key)
        if description is None:
            description = build()
            descriptions[cache_key] = description
        return description

    def _detach_annotation_descriptions(self, names: Iterable[str]) -> None:
        """Gives a clone about to set the annotations ``names`` descriptions of its own - its
        original keeps seeing only descriptions of its own annotations. Replacing an annotation
        empties them: another annotation's type may depend on it.

        Args:
            names: The annotation names about to be set.
        """
        descriptions = self._annotation_descriptions
        if descriptions is None:
            return
        if any(name in self._annotations for name in names) or not descriptions:
            self._annotation_descriptions = None
        else:
            own_descriptions = LookupInfoBuilder.annotation_descriptions.new_shared_bucket()
            own_descriptions.update(descriptions)
            self._annotation_descriptions = own_descriptions

    def _check_selected_names(self, names: Iterable[str], method_name: str) -> None:
        """Checks the first name of every selected path right away: a field, ``pk``, a relation
        or an annotation. The rest of a path is checked when the query is built.

        Args:
            names: The selected paths.
            method_name: The method, named in the error.

        Raises:
            FieldError: A path names no field of the model.
        """
        fields_map = self.model._meta.fields_map
        for name in names:
            first_name = name.partition("__")[0]
            if first_name != "pk" and first_name not in fields_map and first_name not in self._annotations:
                model_name = self.model.__name__
                raise FieldError(
                    f"{model_name}.objects.{method_name}('{name}'): {model_name} has no field '{first_name}'"
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
        self._raise_if_combined("filter")
        return self._filter_or_exclude(False, args, kwargs)

    @CallsBeforeSetup.recorded
    def exclude(self, *args: Q | Exists, **kwargs: Any) -> Self:
        """
        Same as .filter(), but excludes the rows matching all given conditions together -
        ``exclude(a=1, b=2)`` is ``NOT (a = 1 AND b = 2)``, like ``exclude(Q(a=1, b=2))``.
        """
        self._raise_if_combined("exclude")
        return self._filter_or_exclude(True, args, kwargs)

    def _get_field_may_be_null(self, field_name: str) -> bool:
        """Whether ordering by ``field_name`` can meet NULLs: a nullable column, or anything that is
        not a plain column of this model (a related-model path - its LEFT JOIN yields NULLs - or an
        annotation)."""
        field_object = self.model._meta.fields_map.get(field_name)
        return field_object is None or field_object.null

    def _parse_orderings(
        self,
        orderings: tuple[str | Ordering, ...],
        reverse: bool = False,
        nulls_last_by_default: bool = False,
    ) -> list[tuple[str, Order]]:
        """
        Convert ordering from strings/``Ordering`` objects to standard items for queryset.

        Args:
            orderings: What columns/order to order by
            reverse: Whether reverse order
            nulls_last_by_default: Whether an item with no explicit NULL placement, ordering a
                column that can hold NULLs, gets ``NULLS LAST`` - so a NULL never wins
                ``latest()``/``earliest()``. A column that cannot be NULL keeps the plain
                direction (an explicit ``NULLS LAST`` would only cost it index-ordered scans).

        Returns:
            Standard ordering for QuerySet.
        """
        new_ordering = []
        for ordering in orderings:
            field_name, order_type = self._get_ordering_string(ordering, reverse=reverse)
            # "pk" orders by every primary key field, a forward relation by its own key column(s).
            if field_name.partition("__")[0] in self._annotations:
                ordering_info = self._get_annotation_description(
                    "ordering",
                    field_name,
                    lambda: LookupInfoBuilder.get_ordering_info(self.model, field_name, annotations=self._annotations),
                )
            else:
                ordering_info = self.model._meta.get_ordering_info(field_name)
            for ordering_field_name in ordering_info.paths:
                ordering_type = order_type
                if (
                    nulls_last_by_default
                    and ordering_type.nulls_first is None
                    and self._get_field_may_be_null(ordering_field_name)
                ):
                    ordering_type = Order.build(ordering_type.is_ascending, nulls_first=False)
                new_ordering.append((ordering_field_name, ordering_type))
        return new_ordering

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
            return cast("Self", self._get_combined_query()._get_ordered_queryset(orderings))
        self._raise_if_slice_taken("Cannot reorder a query once a slice has been taken.")
        self._forbid_reordering_after_cursor("order_by")
        queryset = self._with_orderings(*orderings)
        queryset._default_ordering_disabled = not orderings
        if all(type(ordering) is str for ordering in orderings):
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
        # A path into a JSON field or annotation orders by an alias of its own, never selected.
        path_annotations = self._get_path_annotations(self._get_ordering_string(ordering)[0] for ordering in orderings)
        if path_annotations:
            queryset._detach_annotation_descriptions(path_annotations)
            queryset._annotations = {**queryset._annotations, **path_annotations}
            queryset._alias_keys = queryset._alias_keys | set(path_annotations)
        queryset._orderings = queryset._parse_orderings(orderings)
        return queryset

    def _with_primary_key_ordering_if_unordered_slice(self) -> Self:
        """This queryset, ordered by the primary key when it is an unordered slice - the rows
        ``first()`` reads, so every method that picks the rows of the slice by primary key
        picks the same ones.

        Returns:
            A clone ordered by the primary key, or this queryset itself.
        """
        if (
            (self._limit is not None or self._offset)
            and not self._apply_default_ordering(self._orderings, self._annotations)
            and not self._distinct_on
        ):
            return self._with_orderings(*self.model._meta.pk_attr_names)
        return self

    def _forbid_reordering_after_cursor(self, method_name: str) -> None:
        """Raises if ``.after_cursor()``/``.before_cursor()`` was already called - the cursor values
        are bound to the ordering active at that point.

        Args:
            method_name: The method replacing or reversing the ordering.

        Raises:
            ValueError: A cursor is already set.
        """
        if self._cursor_values or self._before_cursor_values:
            cursor_method_name = "before_cursor" if self._reverse_result_order else "after_cursor"
            raise QueryError(
                f".{method_name}() cannot be called after .{cursor_method_name}() - its cursor values are "
                f"bound to the ordering that was active when .{cursor_method_name}() was called."
            )

    def _check_cursor_values(self, method_name: str, values: tuple[Any, ...]) -> None:
        """Validates keyset boundary values against the current ordering.

        Args:
            method_name: The calling method, for the error message.
            values: The boundary values.

        Raises:
            ValueError: If there is no ``.order_by()``, or the value count doesn't match it.
            FieldError: If an ordering field is an annotation.
        """
        if not self._orderings:
            raise QueryError(f".{method_name}() requires .order_by() to be called first")
        if len(values) != len(self._orderings):
            raise QueryError(
                f".{method_name}() expects {len(self._orderings)} value(s) to match .order_by(), got {len(values)}"
            )
        for field_name, __ in self._orderings:
            if field_name in self._annotations:
                raise FieldError(f".{method_name}() does not support ordering by an annotation, got '{field_name}'")

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
        self._raise_if_combined("after_cursor")
        self._check_cursor_values("after_cursor", values)
        queryset = self._with_distinct_on_rows_as_subquery()
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
        self._raise_if_combined("before_cursor")
        self._check_cursor_values("before_cursor", values)
        queryset = self._with_distinct_on_rows_as_subquery()
        if not queryset._reverse_result_order:
            queryset._orderings = [(field_name, order.get_reversed()) for field_name, order in self._orderings]
            # A lower boundary from an earlier .after_cursor() becomes the upper one in the
            # reversed ordering.
            queryset._before_cursor_values = self._cursor_values
            queryset._reverse_result_order = True
        queryset._cursor_values = values
        return queryset

    def cursor_values(self, instance: TModel) -> tuple[Any, ...]:
        """The keyset boundary values of ``instance`` for the current ``.order_by(...)`` - what
        ``.after_cursor()``/``.before_cursor()`` take. An ordering across a relation reads the
        related object off ``instance``; a missing relation gives ``None``.

        Args:
            instance: A row of this queryset's model.

        Returns:
            One value per ordering field.

        Raises:
            ValueError: ``.order_by()`` wasn't called, or a related ordering's relation isn't
                loaded.
            FieldError: An ordering field is an annotation, or crosses a to-many relation.
            QueryError: ``instance`` isn't of this queryset's model.
        """
        if not isinstance(instance, self.model):
            raise QueryError(
                f"cursor_values() expects a {self.model.__name__} instance, got {type(instance).__name__}"
            )
        if not self._orderings:
            raise QueryError(".cursor_values() requires .order_by() to be called first")
        values = []
        for field_name, __ in self._orderings:
            if field_name in self._annotations:
                raise FieldError(f".cursor_values() does not support ordering by an annotation, got '{field_name}'")
            values.append(self._get_cursor_value(instance, field_name))
        return tuple(values)

    @staticmethod
    def _get_cursor_value(instance: Model, field_name: str) -> Any:
        """Reads one ordering field's value off ``instance``, following ``__`` relation hops.

        Args:
            instance: The row to read from.
            field_name: A plain or ``related__field`` ordering field name.

        Returns:
            The value, or ``None`` when a relation along the path is NULL.

        Raises:
            ValueError: If a relation along the path isn't loaded.
            FieldError: If the path crosses a to-many relation.
        """
        from hare.models import Model

        current: Any = instance
        *relation_names, last_name = field_name.split("__")
        for relation_name in relation_names:
            relation_field = type(current)._meta.fields_map.get(relation_name)
            if isinstance(relation_field, (BackwardFKRelation, ManyToManyFieldInstance)) and not isinstance(
                relation_field, BackwardOneToOneRelation
            ):
                raise FieldError(
                    f".cursor_values() can't read '{field_name}' - '{relation_name}' is a to-many relation"
                )
            related = getattr(current, relation_name)
            if related is None or related is NoneAwaitable:
                return None
            if not isinstance(related, Model):
                raise QueryError(
                    f".cursor_values() can't read '{field_name}' - relation '{relation_name}' isn't loaded, "
                    "use select_related()/prefetch_related()"
                )
            current = related
        return getattr(current, last_name)

    def _as_single(self) -> QuerySetSingle[TRow | None]:
        if not self._single and (self._limit is not None or self._offset):
            self._is_single_row_of_slice = True
        self._single = True
        self._limit = 1 if self._limit is None else min(self._limit, 1)
        return cast("QuerySetSingle[TRow | None]", self)

    def _with_rows_as_subquery(self) -> Self:
        """A clone restricted to this queryset's rows by ``pk IN (subquery)``, with its own slice and
        ``.distinct(<fields>)`` cleared - a later reordering or filter stays within those rows.

        Returns:
            A plain clone when this queryset is neither sliced nor ``.distinct(<fields>)``.
        """
        queryset = self._clone()
        if self._limit is None and not self._offset and not self._distinct_on:
            return queryset
        self.model._meta.raise_if_no_primary_key("narrowing a sliced or distinct(*fields) queryset's rows")
        pk_attr = self.model._meta.pk_attr
        # A composite key compares its columns as one row value: (a, b) IN (SELECT a, b ...).
        pk_filter_key = "pk__in" if isinstance(pk_attr, tuple) else f"{pk_attr}__in"
        queryset._limit = None
        queryset._offset = None
        queryset._distinct = False
        queryset._distinct_on = []
        queryset._append_filters(False, (), {pk_filter_key: self._get_primary_key_values_query()})
        # A row picked from a slice can't be filtered or reordered any further, as for first().
        queryset._is_single_row_of_slice = self._is_single_row_of_slice or (
            self._limit is not None or bool(self._offset)
        )
        return queryset

    def _with_distinct_on_rows_as_subquery(self) -> Self:
        """A clone restricted to the rows ``.distinct(<fields>)`` picks via a ``pk IN (subquery)``
        filter of the unsliced queryset, keeping its own slice - so a keyset boundary applies to
        those rows, not to the rows ``DISTINCT ON`` picks from.

        Returns:
            A plain clone when this queryset isn't ``.distinct(<fields>)``.
        """
        if not self._distinct_on:
            return self._clone()
        unsliced_queryset = self._clone()
        unsliced_queryset._limit = None
        unsliced_queryset._offset = None
        queryset = unsliced_queryset._with_rows_as_subquery()
        queryset._limit = self._limit
        queryset._offset = self._offset
        return queryset

    @staticmethod
    @ModelCache.fact()
    def _model_takes_direct_get(model: type[Model]) -> bool:
        """Whether ``model``'s queries have nothing added by default but the filters of
        ``Meta.tenant_field``/``Meta.soft_delete_field`` - no custom ``get_queryset()`` and no
        relation loaded by default (``lazy="joined"``/``"select"``).

        Args:
            model: The model.

        Returns:
            True when a ``get()`` on it may run on the plan of its filters directly.
        """
        row_scopes = RowScopes.of(model)
        if len(row_scopes.scopes) > len(row_scopes.field_scopes):
            return False
        meta = model._meta
        for field_name in meta.fk_fields | meta.o2o_fields | meta.m2m_fields:
            if getattr(meta.fields_map[field_name], "lazy", None) in (
                RelationLoadStrategy.JOINED,
                RelationLoadStrategy.SELECT,
            ):
                return False
        return True

    def _get_direct_get(self, kwargs: dict[str, Any]) -> DirectGet | None:
        """What ``get(**kwargs)``/``get_or_none(**kwargs)`` of a queryset nothing else changed runs on:
        the statement plan found by the filter keys, the types of their values and the default
        scope. The first query of a key has no plan yet and is built as usual.

        Args:
            kwargs: The filters.

        Returns:
            The direct get - None when the queryset is built as usual.
        """
        options = self._options
        lock_structure = None
        if options is not QueryOptions.DEFAULT:
            # A queryset whose only option is select_for_update() runs on the plan of its lock.
            if not options.without(*SELECT_FOR_UPDATE_OPTION_NAMES).is_default():
                return None
            lock_structure = tuple([getattr(options, name) for name in SELECT_FOR_UPDATE_OPTION_NAMES])
        if not (
            not self._q_object_list
            and not self._pending_filter_calls
            and not self._annotations
            and not self._select_related
            and not self._prefetch_map
            and not self._prefetch_queries
            and not self._orderings
            and self._limit is None
            and not self._offset
            and not self._distinct
            and not self._is_none
            and self._selection is None
            and self._combination is None
        ):
            return None
        model = self.model
        if not model._meta.basetable.get_table_name():
            # Not set up by Hare.init() - the queryset raises the error for it.
            return None
        db = self._db or self.get_connection()
        if not db.features.supports_positional_rows or not self._model_takes_direct_get(model):
            return None
        if RowScopes.of(model).scopes:
            scope = self._get_direct_get_scope()
            if scope is None:
                return None
            scope_description, scope_keys = scope
        else:
            scope_description, scope_keys = PlanDescription.EMPTY, ()
        # The plan key: the connection, the lock, the filter keys in order, each value's structure,
        # the zone of aware datetimes and, last, the default scope's structure. The model is left out
        # - the plan is looked up in the model's own plans.
        plan_key = (
            "get",
            type(self),
            db.dialect,
            db.connection_name,
            lock_structure,
            tuple(kwargs),
            tuple([len(value) if isinstance(value, (list, tuple, set)) else type(value) for value in kwargs.values()]),
            Timezone.get_rendered_zone_name(),
            scope_description.structure,
        )
        return DirectGet(
            kwargs, plan_key, StatementPlans.plans.get_for_model(model, plan_key), db, scope_description, scope_keys
        )

    def _get_direct_get_scope(self) -> tuple[PlanDescription, tuple[str, ...]] | None:
        """The default scope a direct ``get()`` runs under now.

        Returns:
            Its description and the filter key of each of its values (``RowScopes``); None when it
            can't be resolved now - no tenant is active.
        """
        row_scopes = RowScopes.of(self.model)
        if not row_scopes.scopes:
            return PlanDescription.EMPTY, ()
        return row_scopes.get_filters_plan_description(
            self._visibility.get_for_active_tenant(), uses_default_scope=self._uses_default_scope
        )

    def _apply_direct_get_filters(self) -> None:
        """Adds the filters a direct ``get()`` hasn't added yet - the queryset is changed, built
        into another query or shown, so it is built as usual."""
        direct_get = cast("DirectGet", self._direct_get)
        self._direct_get = None
        if direct_get.plan is not None:
            self._append_filters(False, (), direct_get.filters)

    def _get_single_queryset(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        exception: type[BaseException] | BaseException | None,
        raise_does_not_exist: bool,
    ) -> Self:
        """The queryset of the one row matching the conditions.

        Args:
            args: ``Q`` conditions.
            kwargs: Filter keyword arguments.
            exception: Raised instead of ``DoesNotExist`` when no row matches.
            raise_does_not_exist: Raise when no row matches.

        Returns:
            The single-row queryset.
        """
        direct_get = self._get_direct_get(kwargs) if kwargs and not args else None
        if direct_get is not None and direct_get.plan is not None:
            # Runs on the plan - the filters are added only when the queryset is built after all.
            # _clone(), without its call.
            queryset = self._clone_keeping_calls()
            queryset._call_signature = None
        else:
            filtered_queryset = self.filter(*args, **kwargs) if args or kwargs else self
            queryset = filtered_queryset._with_rows_as_subquery()
            if direct_get is None and queryset is not filtered_queryset:
                filtered_queryset._continue_call_signature(queryset, ("get",))
        queryset._direct_get = direct_get
        queryset._limit = GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK
        queryset._single = True
        queryset._raise_does_not_exist = raise_does_not_exist
        if exception is not None or queryset._options is not QueryOptions.DEFAULT:
            queryset._does_not_exist_exception = exception
        return queryset

    def _await_direct_get(self, direct_get: DirectGet) -> Generator[Any, None, Any]:
        """Runs a direct ``get()``: on its plan with the filters' values bound, or - the first
        query of its key - as usual, keeping the plan found or recorded under the key.

        Args:
            direct_get: The direct get (``_get_direct_get()``).

        Returns:
            The awaitable's generator.
        """
        scope_description = direct_get.scope_description
        scope_keys = direct_get.scope_keys
        if self.model._meta.tenant_field:
            # The tenant is the one active now, as for a query built now.
            scope = self._get_direct_get_scope()
            if scope is None or scope[0].structure != scope_description.structure:
                # A default scope of another structure than when get() was called - built as usual.
                self._apply_direct_get_filters()
                return self.__await__()
            scope_description, scope_keys = scope
        if self._select_for_update and not isinstance(direct_get.db, TransactionClient):
            # Refused by the query run as usual.
            self._apply_direct_get_filters()
            return self.__await__()
        plan = direct_get.plan
        if plan is None:
            return self._fetch_keeping_direct_get_plan(direct_get.plan_key, scope_keys).__await__()
        db = direct_get.db
        # The plan's own LIMIT is the one of whichever query recorded it - a get() reads two rows.
        parameters = plan.bind(
            [*scope_description.values, *direct_get.filters.values()],
            self.model,
            db.dialect,
            GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK,
            None,
        )
        if parameters is None:
            # A value the plan can't bind - built as usual.
            self._apply_direct_get_filters()
            return self.__await__()
        StatementPlans.count_hit()
        return self._fetch_on_plan(plan, parameters, db).__await__()

    def _await_call_signature_rows(self, db: DatabaseClient) -> Generator[Any, None, Any] | None:
        """Runs this queryset of model instances, made by simple calls alone, on the plan kept under
        the key of its calls (``CallSignaturePlans``) - no query is made.

        Args:
            db: The connection it runs on.

        Returns:
            The awaitable's generator; None when there is no plan to run on yet, or the model's rows
            take more than the plan (relations loaded by default, a custom manager) - the queryset
            is then run as usual.
        """
        model = self.model
        if not self._model_takes_direct_get(model) or not db.features.supports_positional_rows:
            return None
        signature_key = CallSignaturePlans.get_key(ModelRowsQuery, self, db, ())
        if signature_key is None:
            return None
        key, values, _value_keys, _visibility = signature_key
        plan = CallSignaturePlans.find(model, key)
        if plan is None or plan.sql is None or plan.decode_plan is None:
            return None
        if plan.join_conditions:
            join_condition_values = plan.get_join_condition_values()
            if join_condition_values is None:
                return None
            values += join_condition_values
        parameters = plan.bind(values, model, db.dialect, self._limit, self._offset)
        if parameters is None:
            return None
        StatementPlans.count_hit()
        return self._fetch_on_plan(plan, parameters, db).__await__()

    async def _fetch_keeping_direct_get_plan(self, plan_key: tuple[Any, ...], scope_keys: tuple[str, ...]) -> Any:
        """Runs the queryset - the first query of a direct ``get()`` key - and keeps the plan it
        found or recorded under that key, when later queries can run on it directly.

        Args:
            plan_key: The key.
            scope_keys: The filter keys of the default scope's values, bound before the ``get()``'s.

        Returns:
            The instance, or None for a ``get_or_none()`` that matched no row.
        """
        kwargs = cast("DirectGet", self._direct_get).filters
        query = self._get_model_rows_query()
        query._make_query_to_run()
        plan = query._statement_plan
        if (
            plan is not None
            and plan.sql is not None
            # Rows read by position, every column of the model and no joined relation.
            and plan.decode_plan is not None
            and plan.decode_plan_key is not None
            and not plan.decode_plan_is_partial
            # One value per filter, as given - a filter the queryset rewrote into other keys
            # (a composite primary key) binds other values.
            and tuple([key for key, _ref in plan.value_refs]) == (*scope_keys, *kwargs)
        ):
            StatementPlans.record((self.model, *plan_key), plan)
        return await query._execute_with_retry_context(query._execute())

    async def _fetch_on_plan(self, plan: StatementPlan, parameters: list[Any], db: DatabaseClient) -> Any:
        """Runs a plan's statement with this queryset's values bound and reads its rows - the run of a
        direct ``get()`` and of a queryset made by simple calls alone.

        Args:
            plan: The plan.
            parameters: The parameters (``StatementPlan.bind()``).
            db: The connection.

        Returns:
            The instances; for a single-row queryset the instance, or None for a ``first()``/
            ``get_or_none()`` that matched no row.
        """
        model = self.model
        decode_plan = plan.decode_plan
        reader = None
        if decode_plan is not None and HydrateAccelerator.module is not None:
            # The model's columns alone, in its own order - read by the native reader at once.
            reader = HydrateAccelerator.get_model_reader(
                model, decode_plan, plan.decode_plan_is_partial, db.dialect.types, Timezone.get_aware_zone_name()
            )
        sql: str = plan.sql  # type: ignore[assignment]  # a plan run here always has its SQL
        # A read - retried on a lost connection, like every queryset read.
        token = retryable_read_query_active.set(True) if db.read_retry_max_retries else None
        try:
            if reader is None:
                instances = await self._get_plan_model_rows(plan, db).fetch(sql, parameters)
            else:
                _, rows = await db.execute(sql, parameters, returns_rows=True)
                instances = []
                if rows:
                    try:
                        instances = reader.read(
                            rows if type(rows) is list else list(rows), db.connection_name, 0, False
                        )
                    except TypeError, AttributeError:
                        # A stale build, possibly - the pure-Python read settles it.
                        instances = await self._get_plan_model_rows(plan, db).read_all(rows)
        finally:
            if token is not None:
                retryable_read_query_active.reset(token)
        if not self._single:
            return instances
        if len(instances) == 1:
            return instances[0]
        if instances:
            raise MultipleObjectsReturned(self.model)
        if self._raise_does_not_exist:
            self._raise_object_does_not_exist()
        return None

    def _get_plan_model_rows(self, plan: StatementPlan, db: DatabaseClient) -> ModelRows:
        """The reader of a plan's model rows - ``_fetch_on_plan()`` without the native reader.

        Args:
            plan: The plan.
            db: The connection.

        Returns:
            The reader.
        """
        return ModelRows(
            self.model,
            db,
            select_related_buckets=list(plan.select_related_idx),
            decode_plan=plan.decode_plan,
            decode_plan_is_partial=plan.decode_plan_is_partial,
        )

    def _raise_if_sliced_values_rows_differ(self, method_name: str) -> None:
        """Rejects re-selecting within a slice of a queryset selecting values whose rows aren't one
        per model row - grouped, deduplicated or multiplied by a to-many relation.

        Args:
            method_name: The calling method, for the message.

        Raises:
            QueryError: Such a sliced queryset.
        """
        if self._selection is not None and (self._limit is not None or self._offset):
            self._get_values_query()._raise_if_sliced_rows_differ_from_source(method_name)

    def _get_primary_key_values_query(self) -> ValuesQuery:
        """The primary key of each row this queryset returns, as a query to embed - sliced,
        ordered and ``.distinct()`` exactly like the rows themselves: a plain ``.distinct()``
        dedupes over the model columns plus the ordering columns, as the model query does.

        Returns:
            A ``values_list()`` query of the primary key column(s), flat for a single-column key.
        """
        rows_queryset = self._get_values_rows_queryset()
        if distinct_annotation_names := self._get_distinct_row_multiplying_annotation_names():
            # Ordered last by them, so the DISTINCT over the ordering columns keeps them apart too.
            rows_queryset._orderings = [
                *self._apply_default_ordering(self._orderings, self._annotations),
                *((annotation_name, Order.ASC) for annotation_name in distinct_annotation_names),
            ]
        pk_attr_names = self.model._meta.pk_attr_names
        values_queryset = rows_queryset.values_list(*pk_attr_names, flat=len(pk_attr_names) == 1)
        return values_queryset._with_selection(
            replace(cast("ValuesSelection", values_queryset._selection), distinct_over_ordering_columns=True)
        )._get_values_query()

    def _get_values_rows_queryset(self) -> Self:
        """A clone selecting the same rows with nothing ``values_list()`` rejects - no
        ``.only()``/``.defer()``, ``select_related()``, ``prefetch_related()``, single-row narrowing
        or ``.group_by()``.

        Returns:
            The clone.
        """
        rows_queryset = self._clone()
        rows_queryset._group_bys = ()
        rows_queryset._prefetch_map = {}
        rows_queryset._prefetch_queries = {}
        rows_queryset._fields_for_select = ()
        rows_queryset._deferred_fields = ()
        rows_queryset._select_related = set()
        rows_queryset._explicitly_select_related = set()
        rows_queryset._single = False
        rows_queryset._raise_does_not_exist = False
        rows_queryset._selection = None
        return rows_queryset

    def _get_filter_value_query(self) -> ValuesQuery:
        """The query a filter compares with when this queryset is its value - the values it
        selects, else its primary key, as the filter resolves it.

        Returns:
            The query.
        """
        if self._selection is not None:
            return self._get_values_query()
        return self._get_field_values_query("pk")

    def _get_field_values_query(self, field_name: str) -> ValuesQuery:
        """One field of each row this queryset returns, as a query to embed - a queryset passed as
        a ``__in`` filter value.

        Args:
            field_name: The field to select, ``"pk"`` included.

        Returns:
            A flat ``values_list()`` query of the field.
        """
        return self._get_values_rows_queryset().values_list(field_name, flat=True)._get_values_query()

    def _get_distinct_row_multiplying_annotation_names(self) -> list[str]:
        """The selected annotations reading a to-many relation that a plain ``.distinct()`` keeps
        apart - the rows then repeat a primary key once per distinct value.

        Returns:
            The annotation names, empty when the queryset isn't a plain ``.distinct()`` one.
        """
        if not self._distinct or self._distinct_on or not self._annotations:
            return []
        return self._get_row_multiplying_annotation_names(selected_only=True)

    @CallsBeforeSetup.recorded_result
    def latest(self, *orderings: str | Ordering) -> QuerySetSingle[TRow | None]:
        """Returns the most recent object - the first in the descending ordering by the given fields. A
        plain field name gets ``NULLS LAST``; an ``Ordering`` is reversed exactly.

        Args:
            orderings: Fields to order by.

        Raises:
            FieldError: A field is unknown, or no field is given.
        """
        self._raise_if_combined("latest")
        self._raise_if_sliced_values_rows_differ("latest")
        if not orderings:
            raise FieldError("No fields passed")
        self._forbid_reordering_after_cursor("latest")
        queryset = self._with_rows_as_subquery()
        queryset._orderings = self._parse_orderings(orderings, reverse=True, nulls_last_by_default=True)
        return queryset._as_single()

    @CallsBeforeSetup.recorded_result
    def earliest(self, *orderings: str | Ordering) -> QuerySetSingle[TRow | None]:
        """Returns the earliest object - the first in the ascending ordering by the given fields. A
        plain field name gets ``NULLS LAST``; an ``Ordering`` is used as given.

        Args:
            orderings: Fields to order by.

        Raises:
            FieldError: A field is unknown, or no field is given.
        """
        self._raise_if_combined("earliest")
        self._raise_if_sliced_values_rows_differ("earliest")
        if not orderings:
            raise FieldError("No fields passed")
        self._forbid_reordering_after_cursor("earliest")
        queryset = self._with_rows_as_subquery()
        queryset._orderings = self._parse_orderings(orderings, nulls_last_by_default=True)
        return queryset._as_single()

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
    def __getitem__(self, key: slice | int) -> QuerySet[TModel, TRow] | QuerySetSingle[TRow]:
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
        names (PostgreSQL only) ``DISTINCT ON (fields)``, which keeps one row per combination of
        them - an ``order_by()`` must then begin with the same fields.

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
        self._raise_if_combined("distinct")
        self._raise_if_slice_taken("Cannot create distinct fields once a slice has been taken.")
        queryset = self._clone()
        # A path into a JSON field or a date part (``created__year``) is an alias of its own, never
        # selected.
        path_annotations = self._get_path_annotations(args)
        if path_annotations:
            queryset._detach_annotation_descriptions(path_annotations)
            queryset._annotations = {**queryset._annotations, **path_annotations}
            queryset._alias_keys = queryset._alias_keys | set(path_annotations)
        queryset._distinct = True
        queryset._distinct_on = list(args)
        return queryset

    @CallsBeforeSetup.recorded
    def union(self, *other_qs: QuerySet[Any, Any], all: bool = False) -> Self:
        """Returns the queryset of the combined rows (``UNION``). Chained set operations combine each
        new queryset with the whole result so far. Model querysets combine with model querysets,
        values querysets with values querysets - the rows take the shape of the first one.

        Args:
            other_qs: The querysets to combine with.
            all: Keep duplicate rows (``UNION ALL``).

        Raises:
            QueryError: Model instances are combined with ``.values()``/``.values_list()`` rows.
        """
        return self._combine(other_qs, SetOperation.UNION_ALL if all else SetOperation.UNION)

    @CallsBeforeSetup.recorded
    def intersection(self, *other_qs: QuerySet[Any, Any]) -> Self:
        """
        Return the intersection of QuerySets - the rows every queryset returns (SQL INTERSECT).

        Args:
            other_qs: Another QuerySet(s) to intersect with.

        Returns:
            The queryset of the combined rows.

        Raises:
            QueryError: Model instances are combined with ``.values()``/``.values_list()`` rows.
        """
        return self._combine(other_qs, SetOperation.INTERSECT)

    @CallsBeforeSetup.recorded
    def difference(self, *other_qs: QuerySet[Any, Any]) -> Self:
        """
        Return the difference of QuerySets - this queryset's rows no other one returns (SQL EXCEPT).

        Args:
            other_qs: Another QuerySet(s) to subtract.

        Returns:
            The queryset of the combined rows.

        Raises:
            QueryError: Model instances are combined with ``.values()``/``.values_list()`` rows.
        """
        return self._combine(other_qs, SetOperation.EXCEPT_OF)

    def _combine(self, other_querysets: tuple[Any, ...], set_operation: SetOperation) -> Self:
        """The queryset of this queryset's rows combined with other querysets' rows.

        Args:
            other_querysets: The other querysets.
            set_operation: The operation combining each of them with the whole result so far.

        Returns:
            The queryset.

        Raises:
            QueryError: A branch isn't a queryset, or model instances are combined with
                ``.values()``/``.values_list()`` rows.
        """
        selects_values = self._selects_values()
        for other_queryset in other_querysets:
            if not isinstance(other_queryset, QuerySet):
                raise QueryError(
                    f"union()/intersection()/difference() combine querysets, got {type(other_queryset).__name__}"
                )
            if other_queryset._selects_values() != selects_values:
                raise QueryError(
                    "Cannot combine model instances with .values()/.values_list() rows - call .values()/"
                    ".values_list() on every branch (the rows then take the first branch's shape)."
                )
        combined_rows_are_one_branch = selects_values and bool(
            self._orderings or self._limit is not None or self._offset or self._is_none
        )
        if self._combination is not None and not combined_rows_are_one_branch:
            queryset = self._clone()
            queryset._combination = self._combination.with_branches(other_querysets, set_operation)
            return queryset
        # A queryset of the combined rows on this queryset's connection - this queryset, the
        # combined rows so far when they are ordered, sliced or empty, is its first branch.
        combined_queryset = type(self)(self.model)
        combined_queryset._apply_db(self._db)
        combined_queryset._db_explicitly_chosen = self._db_explicitly_chosen
        combined_queryset._router_fallback_db = self._router_fallback_db
        combined_queryset._instance_connection_name = self._instance_connection_name
        combined_queryset._visibility = self._visibility
        combined_queryset._combination = Combination.of((self, *other_querysets), set_operation)
        return combined_queryset

    def _selects_values(self) -> bool:
        """Whether the rows are the values ``.values()``/``.values_list()`` select - this queryset's,
        or the first of the querysets it combines."""
        if self._combination is not None:
            return self._combination.branches[0]._selects_values()
        return self._selection is not None

    def _get_combined_query(self) -> CombinedQuery:
        """The query building the rows this queryset combines."""
        from hare.query.statements.select.combined_query import CombinedQuery

        return CombinedQuery(self)

    def _raise_if_combined(self, method_name: str) -> None:
        """Rejects a method changing which rows a queryset matches on a queryset combining
        querysets, like Django.

        Args:
            method_name: The method.

        Raises:
            QueryError: The queryset combines querysets.
        """
        if self._combination is not None:
            raise QueryError(
                f"{method_name}() can't be used on a union()/intersection()/difference() - call it on each "
                "queryset before combining them."
            )

    @CallsBeforeSetup.recorded
    def with_cte(self, name: str, query: AwaitableQuery[Any] | Selectable) -> Self:
        """Attaches a named ``WITH`` to the queryset's SQL - recursive when ``query`` references its
        own name. The queryset still selects from the model's table; reference the CTE from a filter
        (``pk__in=RawSQL(...)``), a ``Subquery`` or hand-built SQL.

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
                    .filter(id__in=RawSQL('SELECT id FROM "ancestors"'))
                )
        """
        self._raise_if_combined("with_cte")
        queryset = self._clone()
        # A queryset selecting values is built by its values query.
        cte_body = query._get_compiler() if isinstance(query, QuerySpec) else query
        queryset._with_ctes = (*queryset._with_ctes, (name, cte_body))
        return queryset

    @CallsBeforeSetup.recorded
    def select_for_update(
        self,
        nowait: bool = False,
        skip_locked: bool = False,
        of: tuple[str, ...] = (),
        no_key: bool = False,
    ) -> Self:
        """
        Make QuerySet select for update.

        Returns a queryset that will lock rows until the end of the transaction,
        generating a SELECT ... FOR UPDATE SQL statement on supported databases.

        Args:
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
            QueryError: ``nowait`` and ``skip_locked`` are both set; or, when the query is built,
                an ``of`` name is not a forward relation path, isn't joined by the query, or
                crosses a relation that can't be INNER JOINed.
            UnSupportedError: when the query runs, the database has no SELECT ... FOR UPDATE
                equivalent.
        """
        self._raise_if_combined("select_for_update")
        if nowait and skip_locked:
            raise QueryError("select_for_update() options nowait and skip_locked are mutually exclusive")
        queryset = self._clone()
        queryset._select_for_update = True
        queryset._select_for_update_nowait = nowait
        queryset._select_for_update_skip_locked = skip_locked
        queryset._select_for_update_of = set(of)
        queryset._select_for_update_no_key = no_key
        return queryset

    @CallsBeforeSetup.recorded
    def annotate(self, **kwargs: Expression | Term) -> Self:
        """
        Annotate result with aggregation or function result.

        Raises:
            TypeError: Value of kwarg is expected to be a ``Function`` instance.
            FieldError: A key is a field of the model or ``pk``.
        """
        self._raise_if_combined("annotate")
        self._raise_if_not_expressions(kwargs)
        self._raise_if_annotation_names_collide_with_fields(kwargs)
        queryset = self._clone()
        queryset._detach_annotation_descriptions(kwargs)
        for key, annotation in kwargs.items():
            queryset._annotations[key] = annotation
            # A previous .alias(key=...) on this same key is superseded by a real .annotate() -
            # the key should now behave like an ordinary annotation (selected by default).
            queryset._alias_keys = queryset._alias_keys - {key}
        if queryset._selection is not None:
            queryset._selection = queryset._selection.with_added_names(kwargs)
        return queryset

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
        self._raise_if_combined("alias")
        self._raise_if_not_expressions(kwargs)
        self._raise_if_annotation_names_collide_with_fields(kwargs)
        queryset = self._clone()
        queryset._detach_annotation_descriptions(kwargs)
        for key, annotation in kwargs.items():
            queryset._annotations[key] = annotation
            queryset._alias_keys = queryset._alias_keys | {key}
        if queryset._selection is not None:
            queryset._selection = queryset._selection.with_added_names((), kwargs)
        return queryset

    @staticmethod
    def _raise_if_not_expressions(annotations: dict[str, Any]) -> None:
        """Rejects an annotation value that isn't an expression.

        Raises:
            TypeError: A value is e.g. a bare field name string.
        """
        for key, annotation in annotations.items():
            if not isinstance(annotation, (Expression, Term)):
                hint = f" - use F({annotation!r}) to reference a field" if isinstance(annotation, str) else ""
                value_type_name = type(annotation).__name__
                raise TypeError(
                    f"The annotation {key!r} must be an expression, got {value_type_name} {annotation!r}{hint}"
                )

    def _raise_if_annotation_names_collide_with_fields(self, annotation_names: Iterable[str]) -> None:
        """Rejects an annotation named after a field of the model - every other reference to
        that name (another annotation's F(), a filter, an ordering) would silently read the
        annotation instead of the field.

        Args:
            annotation_names: The new annotation names.

        Raises:
            FieldError: A name is a field of the model or ``pk``.
            ValueError: A name contains quotes, a semicolon, whitespace or an SQL comment.
        """
        annotation_names = list(annotation_names)
        self._raise_if_annotation_names_are_unsafe(annotation_names)
        colliding_names = sorted(
            name for name in annotation_names if name in self.model._meta.fields_map or name == "pk"
        )
        if colliding_names:
            raise FieldError(
                f"Annotation name(s) {colliding_names} conflict with field(s) of model "
                f"{self.model.__name__} - references to the field would read the annotation instead. "
                "Use a different annotation name."
            )

    def _get_path_annotations(self, names: Iterable[str]) -> dict[str, Expression]:
        """The selected names reading a path into a JSON field or annotation, through an array or range
        field, or a part of a date, time or datetime field - each as an annotation of its own name.

        Args:
            names: The selected names.

        Returns:
            Each path name to its expression.
        """
        paths: dict[str, Expression] = {}
        for name in names:
            base_name, __, path = name.partition("__")
            if not path or name in self._annotations:
                continue
            if base_name in self._annotations or LookupPaths.get_value_path_split(self.model, name) is not None:
                paths[name] = F(name)
            elif (date_part_split := LookupPaths.get_date_part_split(self.model, name)) is not None:
                field_path, part = date_part_split
                paths[name] = Trunc(field_path, part) if part in DATETIME_CAST_SEGMENTS else Extract(field_path, part)
        return paths

    @CallsBeforeSetup.recorded
    def group_by(self, *fields: str) -> Self:
        """
        Make QuerySet returns list of dict or tuple with group by - before or after
        ``.values()``/``.values_list()``.
        """
        self._raise_if_combined("group_by")
        queryset = self._clone()
        queryset._group_bys = fields
        return queryset

    def _raise_if_select_related_unused_by_values(self, method_name: str, result_type: str) -> None:
        """Rejects an explicit ``select_related()`` relation that ``.values()``/``.values_list()``
        would ignore - one carrying no ``Select(..., extra_condition=...)`` and not the parent path
        of one.

        Args:
            method_name: The calling method, for the message.
            result_type: What the method returns ("dicts" / "tuples").

        Raises:
            ValueError: A relation has no effect on the result.
        """
        extra_condition_paths = self._select_related_extra_conditions.keys()
        for relation_path in sorted(self._explicitly_select_related):
            if relation_path in extra_condition_paths or any(
                extra_condition_path.startswith(f"{relation_path}__") for extra_condition_path in extra_condition_paths
            ):
                continue
            raise QueryError(
                f"{method_name} cannot be used with select_related({relation_path!r}) - the result is "
                f"plain {result_type}, not model instances, so there's nothing to attach a joined "
                f'relation to. Select the relation\'s own fields directly (e.g. "{relation_path}__name") '
                "or pass Select(relation, extra_condition=...) to condition that relation's JOIN."
            )

    @staticmethod
    def _raise_if_invalid_values_arguments(
        method_name: str, field_names: tuple[Any, ...], expressions: dict[str, Any], *, allow_field_name_kwargs: bool
    ) -> None:
        """Rejects a ``.values()``/``.values_list()`` argument that is neither a field name nor an
        expression.

        Args:
            method_name: The calling method, for the error message.
            field_names: The positional arguments.
            expressions: The keyword arguments.
            allow_field_name_kwargs: Whether a keyword argument may be a field name string.

        Raises:
            QueryError: An argument has the wrong type.
        """
        for field_name in field_names:
            if not isinstance(field_name, str):
                raise QueryError(f"{method_name} positional arguments must be field names, got {field_name!r}")
        for key, value in expressions.items():
            if isinstance(value, (Expression, Term)) or (allow_field_name_kwargs and isinstance(value, str)):
                continue
            expected = "a field name or an expression" if allow_field_name_kwargs else "an expression"
            raise QueryError(
                f"{method_name} keyword argument {key!r} must be {expected} (F(), Value(), a function, ...), "
                f"got {value!r}"
            )

    @overload
    def values_list(
        self, *fields_: str, flat: Literal[False] = False, named: Literal[False] = False, **kwargs: Expression | Term
    ) -> QuerySet[TModel, tuple[Any, ...]]: ...

    @overload
    def values_list(
        self, *fields_: str, flat: bool = False, named: bool = False, **kwargs: Expression | Term
    ) -> QuerySet[TModel, Any]: ...

    @CallsBeforeSetup.recorded
    def values_list(
        self, *fields_: str, flat: bool = False, named: bool = False, **kwargs: Expression | Term
    ) -> QuerySet[TModel, Any]:
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
            return cast(
                "QuerySet[TModel, Any]",
                self._get_combined_query()._get_values_queryset(
                    lambda branch: branch.values_list(*fields_, flat=flat, named=named, **kwargs)
                ),
            )
        if flat and named:
            raise QueryError(".values_list() can't use flat=True and named=True together.")
        queryset = self._get_values_source_queryset()
        queryset._check_selected_names(fields_, "values_list")
        queryset._raise_if_values_unusable(".values_list()", fields_, kwargs, allow_field_name_kwargs=False)
        names = (*fields_, *kwargs)
        if flat and len(names or queryset._get_every_selected_name()) != 1:
            raise QueryError(".values_list(flat=True) selects exactly one field")
        queryset = queryset.alias(**kwargs) if kwargs else queryset._clone()
        shape = RowShape.FLAT if flat else RowShape.NAMED if named else RowShape.TUPLE
        queryset._selection = ValuesSelection(shape, field_names=names)
        if self._selection is None and not kwargs:
            self._continue_call_signature(queryset, ("values_list", fields_, flat, named))
        return cast("QuerySet[TModel, Any]", queryset)

    @CallsBeforeSetup.recorded
    def values(self, *args: str, **kwargs: str | Expression | Term) -> QuerySet[TModel, dict[str, Any]]:
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
            return cast(
                "QuerySet[TModel, dict[str, Any]]",
                self._get_combined_query()._get_values_queryset(lambda branch: branch.values(*args, **kwargs)),
            )
        queryset = self._get_values_source_queryset()
        queryset._check_selected_names(args, "values")
        queryset._raise_if_values_unusable(".values()", args, kwargs, allow_field_name_kwargs=True)
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
        return cast("QuerySet[TModel, dict[str, Any]]", queryset)

    def _raise_if_values_unusable(
        self,
        method_name: str,
        field_names: tuple[Any, ...],
        expressions: dict[str, Any],
        *,
        allow_field_name_kwargs: bool,
    ) -> None:
        """Rejects a ``.values()``/``.values_list()`` call with arguments of the wrong type, or on a
        queryset choosing model fields or relations to load.

        Args:
            method_name: The calling method, for the error message.
            field_names: The positional arguments.
            expressions: The keyword arguments.
            allow_field_name_kwargs: Whether a keyword argument may be a field name string.

        Raises:
            QueryError: An argument has the wrong type, or the queryset uses ``.only()``,
                ``.defer()``, ``prefetch_related()`` or a ``select_related()`` relation the rows
                don't use.
        """
        self._raise_if_invalid_values_arguments(
            method_name, field_names, expressions, allow_field_name_kwargs=allow_field_name_kwargs
        )
        if self._fields_for_select:
            raise QueryError(f"{method_name} cannot be used with .only()")
        if self._deferred_fields:
            raise QueryError(f"{method_name} cannot be used with .defer()")
        result_type = "dicts" if method_name == ".values()" else "tuples"
        if self._prefetch_map or self._prefetch_queries:
            raise QueryError(
                f"{method_name} cannot be used with prefetch_related() - the result is plain {result_type}, "
                "not model instances, so there's nothing to attach a prefetched relation to."
            )
        self._raise_if_select_related_unused_by_values(method_name, result_type)

    def _get_every_selected_name(self) -> list[str]:
        """The names ``.values()``/``.values_list()`` without arguments select - every stored field
        of the model, then every annotation that isn't an ``.alias()``.

        Returns:
            The names.
        """
        return [field for field in self.model._meta.fields_map if field in self.model._meta.fields_db_projection] + [
            key for key in self._annotations if key not in self._alias_keys
        ]

    def _get_values_source_queryset(self) -> QuerySet[TModel, TModel]:
        """The queryset a ``.values()``/``.values_list()`` call selects from - this one, or, when it
        already selects values, this one selecting model instances again, explicitly grouped by the
        selected fields an aggregate implicitly groups it by.

        Returns:
            The queryset.
        """
        if self._selection is None:
            return cast("QuerySet[TModel]", self)
        return cast("QuerySet[TModel]", self._get_values_query()._get_grouped_source_queryset())

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

    def _get_values_query(self) -> ValuesQuery:
        """The query selecting the rows of this queryset as its ``.values()``/``.values_list()``
        call selects them.

        Returns:
            The query.
        """
        from hare.query.statements.select.values_query import ValuesQuery

        selection = cast("ValuesSelection", self._selection)
        queryset = self
        annotations = queryset._annotations
        if selection.shape is RowShape.DICT:
            if selection.selects_every_field:
                fields_for_select = {name: name for name in queryset._get_every_selected_name()}
            else:
                fields_for_select = {name: name for name in selection.field_names}
                fields_for_select.update(selection.renamed_fields)
                if path_annotations := queryset._get_path_annotations(fields_for_select.values()):
                    annotations = {**annotations, **path_annotations}
            values_query = ValuesQuery(
                queryset,
                selection,
                annotations,
                # A key renaming an annotation (values(key="annotation")) selects that annotation
                # under the key, even when the key was an .alias() before.
                queryset._alias_keys
                - set(fields_for_select.values())
                - {return_as for return_as, field in fields_for_select.items() if field in annotations},
                fields_for_select,
            )
        else:
            if path_annotations := queryset._get_path_annotations(selection.field_names):
                annotations = {**annotations, **path_annotations}
            fields_for_select_list = list(selection.field_names) or queryset._get_every_selected_name()
            values_query = ValuesQuery(
                queryset,
                selection,
                annotations,
                # A named .alias() is selected like any annotation.
                queryset._alias_keys - set(fields_for_select_list),
                fields_for_select_list,
            )
        return values_query

    def _get_model_rows_query(self, for_write: bool = False) -> ModelRowsQuery[TModel]:
        """The query building and running this queryset's model instances once, bound to the
        connection it runs on.

        Args:
            for_write: Whether the connection is chosen for a write.

        Returns:
            The query.
        """
        query = ModelRowsQuery(self)
        query._is_execution_query = True
        if cast("DatabaseClient | None", query._db) is None:
            query._apply_db(query.get_connection(for_write))
        return query

    def _get_compiler(self) -> AwaitableQuery[Any]:
        """The query building this queryset's SQL - the queryset itself for model instances.

        Returns:
            The query.
        """
        CallsBeforeSetup.raise_if_invalid(self)
        return self._get_select_query()

    def _get_select_query(self) -> RowsQuery[Any]:
        """The query of this queryset's rows, made for the caller alone.

        Returns:
            The query of the combined rows, of the selected values, else of model instances.
        """
        if self._direct_get is not None:
            self._apply_direct_get_filters()
        if self._combination is not None:
            return self._get_combined_query()
        if self._selection is not None:
            return self._get_values_query()
        return ModelRowsQuery(self)

    def _get_rows_source(self) -> Self | ValuesQuery | CombinedQuery:
        """What the queries derived from this queryset's rows (count, exists, aggregate, update,
        first/last) are made by.

        Returns:
            The query of the combined rows, of the selected values, else this queryset itself.
        """
        if self._direct_get is not None:
            self._apply_direct_get_filters()
        if self._combination is not None:
            return self._get_combined_query()
        if self._selection is not None:
            return self._get_values_query()
        return self

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
        from hare.query.statements.summary.count_query import CountQuery

        self._raise_if_prefetching("count", "a plain int")
        rows_query = self._get_rows_query_if_rows_repeat()
        count_query = CountQuery(self, rows_query=rows_query)
        if rows_query is None:
            self._hand_call_signature(count_query)
        return count_query

    def _get_exists_query(self) -> ExistsQuery:
        """Whether any row matches.

        Raises:
            QueryError: ``prefetch_related()`` was called.
        """
        from hare.query.statements.summary.exists_query import ExistsQuery

        self._raise_if_prefetching("exists", "a plain bool")
        # Without an offset the repeated rows can't change whether any row exists.
        rows_query = self._get_rows_query_if_rows_repeat() if self._offset else None
        exists_query = ExistsQuery(self, rows_query=rows_query)
        if rows_query is None:
            self._hand_call_signature(exists_query)
        return exists_query

    def _get_rows_query_if_rows_repeat(self) -> ValuesQuery | None:
        """The unsliced primary key query of the rows, when they repeat a primary key or
        ``.distinct(<fields>)`` picks one row per group - ``count()``/``exists()`` count them as
        returned.

        Returns:
            The query, or None when the rows are the plain matching rows.
        """
        orderings = self._apply_default_ordering(self._orderings, self._annotations)
        # A composite primary key has no single column for COUNT(DISTINCT ...) - a .distinct()
        # then counts its distinct rows as a derived table.
        distinct_over_composite_key = self._distinct and self.model._meta.has_composite_primary_key
        if (
            not self._distinct_on
            and not distinct_over_composite_key
            and not (
                orderings
                and any(
                    field_name not in self._annotations
                    and AggregatedMultiValuedPaths.get_multi_valued_paths(self.model, field_name)
                    for field_name, _order in orderings
                )
            )
            and not self._get_distinct_row_multiplying_annotation_names()
        ):
            return None
        unsliced_queryset = self._clone()
        unsliced_queryset._limit = None
        unsliced_queryset._offset = None
        return unsliced_queryset._get_primary_key_values_query()

    def _get_aggregate_query(self, **kwargs: Expression | Term) -> AggregateQuery:
        """Aggregates over the rows - of a slice, restricted to them by their primary key.

        Raises:
            QueryError: The query is sliced and its rows can repeat a primary key.
            QueryError: ``prefetch_related()`` was called.
        """
        from hare.query.statements.summary.aggregate_query import AggregateQuery

        if self._limit is not None or self._offset:
            if ModelRowsQuery(self)._rows_repeat_per_primary_key() or (
                not self._distinct_on and self._get_rows_query_if_rows_repeat() is not None
            ):
                raise QueryError(
                    "aggregate() can't be used on a sliced queryset whose rows can repeat a primary key (a filter, "
                    "annotation or ordering over a to-many relation) - the slice would hold repeated rows the "
                    "aggregate can't tell apart. Aggregate a sliced .values()/.values_list() query instead."
                )
            # An unordered slice holds the first rows by primary key, the rows first() reads.
            rows_queryset = self._with_primary_key_ordering_if_unordered_slice()._with_rows_as_subquery()
            # A .distinct() slice's filters can still join a to-many relation: its rows are then
            # reduced to one per primary key before the metrics run, as an unsliced .distinct().
            rows_queryset._distinct = self._distinct and not self._distinct_on
            return rows_queryset.aggregate(**kwargs)
        self._raise_if_prefetching("aggregate", "a plain dict")
        return AggregateQuery(self, kwargs)

    def _get_update_query(self, **kwargs: Any) -> UpdateQuery:
        """Updates the rows.

        Raises:
            QueryError: ``Meta.soft_delete_field`` or ``Meta.optimistic_lock_field`` is among
                ``kwargs``.
        """
        from hare.query.statements.write.update_query import UpdateQuery

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
                queryset._orderings = [(pk_attr_name, Order.ASC) for pk_attr_name in self.model._meta.pk_attr_names]
            return queryset._as_single()
        self._forbid_reordering_after_cursor("last")
        queryset = self._with_primary_key_ordering_if_unordered_slice()._with_rows_as_subquery()
        effective_orderings = self._apply_default_ordering(queryset._orderings, queryset._annotations)
        if effective_orderings:
            queryset._orderings = [(field, order_type.get_reversed()) for field, order_type in effective_orderings]
        else:
            queryset._orderings = [(pk_attr_name, Order.DESC) for pk_attr_name in self.model._meta.pk_attr_names]
        return queryset._as_single()

    def _raise_if_values_selected(self, method_name: str) -> None:
        """Rejects a method reading or returning model instances on a queryset returning the
        values ``.values()``/``.values_list()`` select.

        Args:
            method_name: The method.

        Raises:
            QueryError: The queryset selects values.
        """
        if self._selection is not None:
            raise QueryError(
                f"Cannot call {method_name}() after .values() or .values_list() - call it on the queryset "
                "before .values()/.values_list() instead."
            )

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Describes this query built into another one.

        Args:
            context: The enclosing query's - this query resolves names against its own
                annotations.

        Returns:
            The description, None for a query that keeps no plan.
        """
        if self._combination is not None:
            return self._get_combined_query().get_plan_description(context)
        if self._selection is not None:
            return self._get_values_query().get_plan_description(context)
        return ModelRowsQuery(self).get_plan_description(context)

    @CallsBeforeSetup.recorded_result
    def delete(self) -> DeleteQuery:
        """
        Delete all objects in QuerySet.

        Raises:
            QueryError: The queryset selects values - call ``delete()`` before ``.values()``, like Django.
        """
        if self._direct_get is not None:
            self._apply_direct_get_filters()
        self._raise_if_combined("delete")
        from hare.query.statements.write.delete_query import DeleteQuery

        self._raise_if_values_selected("delete")

        return DeleteQuery(self)

    @CallsBeforeSetup.recorded_result
    def hard_delete(self) -> HardDeleteQuery:
        """Deletes every matched row for real, even with ``Meta.soft_delete_field`` - a soft-deleted
        row too, when the queryset sees it (``only_deleted().hard_delete()``). Related rows follow
        their ``on_delete``.

        Raises:
            QueryError: The queryset selects values.
        """
        if self._direct_get is not None:
            self._apply_direct_get_filters()
        self._raise_if_combined("hard_delete")
        from hare.query.statements.write.delete_query import HardDeleteQuery

        self._raise_if_values_selected("hard_delete")

        return HardDeleteQuery(self)

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
                ``.restore()`` (single-row only - it does not symmetrically un-cascade whatever
                ``.delete()`` cascaded to related rows) instead. Also raised if
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
        return self._get_rows_source()._get_update_query(**kwargs)

    @CallsBeforeSetup.recorded_result
    def count(self) -> CountQuery:
        """
        Return count of objects in queryset instead of objects.

        Raises:
            ValueError: If prefetch_related() was called earlier in the chain.
        """
        return self._get_rows_source()._get_count_query()

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
            QueryError: The queryset is sliced and its rows can repeat a primary key.
            ValueError: prefetch_related() was called earlier in the chain.
        """
        return self._get_rows_source()._get_aggregate_query(**kwargs)

    @CallsBeforeSetup.recorded_result
    def exists(self) -> ExistsQuery:
        """
        Return True/False whether queryset exists.

        Raises:
            ValueError: If prefetch_related() was called earlier in the chain.
        """
        return self._get_rows_source()._get_exists_query()

    @CallsBeforeSetup.recorded_result
    def contains(self, obj: TModel) -> ContainsQuery:
        """
        Check if the QuerySet contains the given instance.

        Args:
            obj: The model instance to check for.

        Returns:
            True if the QuerySet contains the instance, False otherwise.
        """
        if self._direct_get is not None:
            self._apply_direct_get_filters()
        self._raise_if_combined("contains")
        self._raise_if_values_selected("contains")
        if not isinstance(obj, self.model):
            raise QueryError("The given object is not an instance of the queryset's model.")

        if obj._pk_is_unset():
            raise QueryError("The given object does not have a primary key.")

        if self._limit is not None or self._offset:
            # Membership in a SLICE needs the object's position among the ordered rows - a plain
            # `WHERE pk = ...` has no way to express that, and silently ignoring the slice made
            # contains() answer for the whole table instead (limit(0).contains(obj) was True).
            raise QueryError("contains() can't be used on a sliced queryset (after .limit()/.offset()).")

        from hare.query.statements.summary.contains_query import ContainsQuery

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

    def raw(self, sql: str, params: Sequence[Any] = ()) -> RawSQLQuery[TModel]:
        """
        Return the QuerySet from raw SQL. Any value that varies per call belongs in ``params``
        (substituted at each ``%s`` placeholder in ``sql`` as a real bind parameter), never
        string-interpolated into ``sql`` directly - see ``RawSQL``'s own docstring.
        """
        from hare.query.statements.select.raw_sql_query import RawSQLQuery

        # The SQL is the caller's own - no default scope is worked out for it.
        db = self._db if self._db is not None else self.get_connection()
        return RawSQLQuery(model=self.model, db=db, sql=sql, params=params)

    @CallsBeforeSetup.recorded_result
    def first(self) -> QuerySetSingle[TRow | None]:
        """Limits the queryset to its first object and returns it instead of a list. Without an
        ordering the rows are ordered by the primary key; a ``.group_by()``/``.distinct(<fields>)``
        queryset is left unordered.
        """
        rows_source = self._get_rows_source()
        first_queryset = rows_source._get_first(reverse=False)
        if rows_source is self:
            self._continue_call_signature(cast("Self", first_queryset), ("first",))
        return cast("QuerySetSingle[TRow | None]", first_queryset)

    @CallsBeforeSetup.recorded_result
    def last(self) -> QuerySetSingle[TRow | None]:
        """Limits the queryset to its last object and returns it instead of a list. The ordering is
        reversed exactly, NULL placement included. The rows of an unordered slice are taken by the
        primary key.

        Raises:
            QueryError: The queryset selects values and is sliced over rows that aren't one per
                model row.
        """
        rows_source = self._get_rows_source()
        last_queryset = rows_source._get_first(reverse=True)
        if rows_source is self:
            self._continue_call_signature(cast("Self", last_queryset), ("last",))
        return cast("QuerySetSingle[TRow | None]", last_queryset)

    @CallsBeforeSetup.recorded_result
    def get(
        self, *args: Q, exception: type[BaseException] | BaseException | None = None, **kwargs: Any
    ) -> QuerySetSingle[TRow]:
        """
        Fetch exactly one object matching the parameters.

        Args:
            args: ``Q`` conditions, as ``filter()`` takes them.
            exception: Raised instead of ``DoesNotExist`` when no object matches - either an
                exception class (instantiated with no arguments) or an already-constructed instance
                (raised as-is). Does not affect ``MultipleObjectsReturned``.
            kwargs: Field conditions, as ``filter()`` takes them.

        Raises:
            QueryError: Conditions are given for a sliced queryset.
        """
        if self._combination is not None:
            return cast(
                "QuerySetSingle[TRow]",
                self._get_combined_query()._get_single_queryset(
                    args, kwargs, exception=exception, raise_does_not_exist=True
                ),
            )
        if self._selection is not None:
            self._raise_if_sliced_values_rows_differ("get")
        # The single-row queryset - typed without cast()'s call on the path of every get().
        return self._get_single_queryset(  # type: ignore[return-value]
            args, kwargs, exception=exception, raise_does_not_exist=True
        )

    @staticmethod
    def validate_bulk_objects(
        model: type[Model], objects: list[Any], method_name: str, field_names: Iterable[str] | None = None
    ) -> None:
        """Checks every object handed to ``bulk_create()``/``bulk_update()`` is an instance of
        ``model`` itself and, when loaded with ``.only()``/``.defer()``, has every written field.

        Args:
            model: The model the call writes to.
            objects: The objects to write.
            method_name: Names the call in the raised message.
            field_names: The fields written from each object, or None for every column the model
                inserts.

        Raises:
            QueryError: An object is not an instance of ``model`` - a sibling model or a subclass
                with a table of its own included.
            IncompleteInstanceError: A partially loaded object lacks a written field.
        """
        meta = model._meta
        inserts_every_column = field_names is None
        written_field_names = (
            [field_name for field_name in meta.fields_db_projection if not meta.fields_map[field_name].generated]
            if field_names is None
            else list(field_names)
        )
        for obj in objects:
            if type(obj) is not model:
                raise QueryError(
                    f"{method_name}() on {model.__name__} got a {type(obj).__name__} object - every object "
                    f"must be a {model.__name__} instance"
                )
            if not obj._partial:
                continue
            object_field_names = written_field_names
            if inserts_every_column and obj._custom_generated_pk:
                object_field_names = [*written_field_names, *meta.pk_attr_names]
            missing_field_names = [
                field_name
                for field_name in dict.fromkeys(object_field_names)
                if field_name not in obj._await_when_save and not hasattr(obj, field_name)
            ]
            if missing_field_names:
                raise IncompleteInstanceError(
                    f"{model.__name__} is a partial model, field(s) {missing_field_names} are not loaded - "
                    f"{method_name}() writes them (pk={obj.pk!r})"
                )

    def get_bulk_update_column_field_names(self, field_names: list[str]) -> list[str]:
        """The attributes ``bulk_update()`` reads for ``field_names`` - a forward FK/O2O relation
        is read through its key field(s).

        Args:
            field_names: Validated field names.

        Returns:
            The attribute names.
        """
        column_field_names: list[str] = []
        for field_name in field_names:
            field_object = self.model._meta.fields_map[field_name]
            relation_source_fields = getattr(field_object, "source_fields", None)
            if relation_source_fields and field_name not in self.model._meta.fields_db_projection:
                column_field_names.extend(relation_source_fields)
            else:
                column_field_names.append(field_name)
        return column_field_names

    def get_bulk_update_field_names(self, fields: Iterable[str]) -> list[str]:
        """Validates ``bulk_update()``'s ``fields`` argument.

        Args:
            fields: The field names as the caller passed them.

        Returns:
            The field names, duplicates dropped, in the given order.

        Raises:
            QueryError: ``fields`` is a string or empty, or names the primary key.
            FieldError: A name isn't a field of the model.
        """
        if isinstance(fields, (str, bytes)):
            raise QueryError(
                f"bulk_update() on {self.model.__name__}: fields must be a list of field names, got the "
                f"string {fields!r} - wrap a single name in a list"
            )
        field_names = list(dict.fromkeys(fields))
        if not field_names:
            raise QueryError(f"bulk_update() on {self.model.__name__} needs at least one field to update")
        meta = self.model._meta
        unknown_field_names = [
            field_name for field_name in field_names if field_name != "pk" and field_name not in meta.fields_map
        ]
        if unknown_field_names:
            raise FieldError(
                f"bulk_update() on {self.model.__name__} got unknown field(s) {unknown_field_names} - "
                f"available fields: {sorted(meta.fields_map)}"
            )
        primary_key_field_names = {"pk", *meta.pk_attr_names}
        for field_name in field_names:
            field_object = meta.fields_map.get(field_name)
            relation_source_fields = getattr(field_object, "source_fields", None) or ()
            if field_name in primary_key_field_names or primary_key_field_names.intersection(relation_source_fields):
                raise QueryError(
                    f"bulk_update() on {self.model.__name__} can't update the primary key ('{field_name}') - "
                    "every row is matched by it; update it with QuerySet.update() instead"
                )
        return field_names

    def bulk_create(
        self,
        objects: Iterable[TModel],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_fields: Iterable[str] | None = None,
        on_conflict: Iterable[str] | None = None,
        on_conflict_constraint: str | None = None,
        conflict_where: str | None = None,
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
            conflict_where: The ``WHERE`` of the partial unique index the target matches, as raw
                SQL. Needs ``on_conflict``. Not on SQLite.
            returning: Read the database-generated primary key, generated columns and omitted
                ``db_default`` columns back onto each object. Not on SQLite. With
                ``ignore_conflicts`` it needs ``on_conflict=[...]`` to match the rows. None takes
                ``Meta.returning``. Not with ``use_copy``.
            use_copy: Load the rows through the Postgres ``COPY`` protocol - faster for large
                batches, with no ``RETURNING`` and no conflict handling. On rust_pg ``COPY`` can't
                join a transaction: batches commit one by one.

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
        # Meta.returning is a preference: inherited, it yields to use_copy or a dialect without
        # ordered RETURNING; passed explicitly, a conflict raises.
        returning_explicitly_requested = returning is not None
        returning = self.model._meta.returning if returning is None else returning
        if ignore_conflicts and update_fields:
            raise QueryError(
                "ignore_conflicts and update_fields are mutually exclusive.",
            )
        if on_conflict and on_conflict_constraint:
            raise QueryError("on_conflict and on_conflict_constraint are mutually exclusive.")
        from hare.query.statements.write.bulk_create_query import BulkCreateQuery

        if update_fields is not None:
            update_fields = BulkCreateQuery.get_db_field_names(self.model, update_fields, "update_fields")
        if on_conflict is not None:
            on_conflict = BulkCreateQuery.get_db_field_names(self.model, on_conflict, "on_conflict")
        conflict_target = on_conflict or on_conflict_constraint
        if not ignore_conflicts:
            if (update_fields and not conflict_target) or (conflict_target and not update_fields):
                raise QueryError("update_fields and on_conflict/on_conflict_constraint need set in same time.")
        if update_fields:
            # ON CONFLICT DO UPDATE builds its own SET clause: a generated field can't be written,
            # and updating Meta.tenant_field would move another tenant's row into the caller's.
            update_fields_set = set(update_fields)
            generated_fields = [
                field
                for field in update_fields_set
                if getattr(self.model._meta.fields_map.get(field), "generated", False)
            ]
            if generated_fields:
                raise QueryError(
                    f"bulk_create() on {self.model.__name__} can't target generated field(s) "
                    f"{generated_fields} in update_fields - they're computed by the database, not "
                    "written to."
                )
            tenant_field = self.model._meta.tenant_field
            if tenant_field and update_fields_set.intersection(
                BulkCreateQuery.get_db_field_names(self.model, [tenant_field], "update_fields")
            ):
                raise QueryError(
                    f"Cannot target '{self.model._meta.tenant_field}' via bulk_create()'s "
                    "update_fields - ON CONFLICT DO UPDATE resolves against the table's physical "
                    "unique constraint regardless of tenant scoping, so this would let a caller "
                    "move an existing row into their own tenant."
                )
            if self.model._meta.optimistic_lock_field in update_fields_set:
                raise QueryError(
                    f"Cannot target '{self.model._meta.optimistic_lock_field}' via bulk_create()'s "
                    "update_fields - it's bumped automatically on every conflicting row."
                )
            if self.model._meta.soft_delete_field in update_fields_set:
                raise QueryError(
                    f"Cannot target '{self.model._meta.soft_delete_field}' via bulk_create()'s "
                    "update_fields - use .delete()/.restore() instead, a conflicting row must not "
                    "be soft-deleted without its cascade or restored behind restore()'s back."
                )
        if conflict_where:
            if on_conflict_constraint:
                raise QueryError("conflict_where and on_conflict_constraint are mutually exclusive.")
            if not on_conflict:
                raise QueryError("conflict_where requires on_conflict to name the partial index's own columns.")
        if use_copy and returning:
            if not returning_explicitly_requested:
                returning = False
            else:
                raise UnSupportedError(
                    "use_copy and returning are mutually exclusive - the Postgres COPY "
                    "protocol has no RETURNING support."
                )
        if use_copy and (ignore_conflicts or update_fields or on_conflict or on_conflict_constraint or conflict_where):
            raise UnSupportedError(
                "use_copy does not support ON CONFLICT - COPY performs a pure bulk insert with no upsert semantics."
            )
        # The checks needing the dialect are made when the query is awaited and its connection is
        # known.
        if returning and ignore_conflicts and not on_conflict:
            # Skipped rows aren't returned, so the returned ones are matched by the on_conflict
            # columns - required up front, not found out when a conflict happens.
            if not returning_explicitly_requested:
                returning = False
            else:
                raise QueryError(
                    "bulk_create(ignore_conflicts=True, returning=True) requires on_conflict=[...] "
                    "naming the conflict target columns - a row skipped by ON CONFLICT DO NOTHING "
                    "never appears in RETURNING, so the surviving rows can't be matched back to "
                    "their objects without them (a named on_conflict_constraint isn't enough)."
                )
        if batch_size is not None and batch_size <= 0:
            raise QueryError(f"bulk_create(): batch_size must be a positive integer, got {batch_size!r}")
        objects = list(objects)
        self.validate_bulk_objects(self.model, objects, "bulk_create")
        tenant_scoped = bool(self.model._meta.tenant_field) and not self._visibility.all_tenants
        if tenant_scoped:
            if update_fields:
                # ON CONFLICT resolves against the table's physical unique constraint regardless
                # of tenant - the DO UPDATE is limited to the active tenant's rows, so a
                # conflicting row of another tenant is left untouched (and its object unwritten).
                if returning and not on_conflict:
                    if not returning_explicitly_requested:
                        returning = False
                    else:
                        raise QueryError(
                            f"bulk_create(update_fields=..., returning=True) on {self.model.__name__} "
                            "requires on_conflict=[...] naming the conflict target columns - a row of "
                            "another tenant is skipped by the tenant-limited ON CONFLICT DO UPDATE and "
                            "never appears in RETURNING, so the surviving rows can only be matched back "
                            "to their objects through them."
                        )

        return self._share_ambient_scope(
            BulkCreateQuery(
                db=self._db,
                model=self.model,
                objects=objects,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_fields=update_fields,
                on_conflict=on_conflict,
                on_conflict_constraint=on_conflict_constraint,
                conflict_where=conflict_where,
                returning=returning,
                returning_explicitly_requested=returning_explicitly_requested,
                use_copy=use_copy,
                tenant_scoped=tenant_scoped,
            )
        )

    def bulk_update(
        self,
        objects: Iterable[TModel],
        fields: Iterable[str],
        batch_size: int | None = None,
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
                ``.restore()`` (single-row only - it does not symmetrically un-cascade whatever
                ``.delete()`` cascaded to related rows) instead; if ``Meta.optimistic_lock_field`` is among
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
        returning = self.model._meta.returning if returning is None else returning
        objects = list(objects)
        fields = self.get_bulk_update_field_names(fields)
        self.validate_bulk_objects(
            self.model,
            objects,
            "bulk_update",
            [*self.get_bulk_update_column_field_names(fields), *self.model._meta.pk_attr_names],
        )
        tenant_field = self.model._meta.tenant_field
        tenant_scoped = bool(tenant_field) and not self._visibility.all_tenants
        if tenant_scoped:
            # An async default= of tenant_field is resolved only by save() - its value isn't there
            # to compare with the active tenant yet.
            pending_pks = [obj.pk for obj in objects if tenant_field in obj._await_when_save]
            if pending_pks:
                raise QueryError(
                    f"bulk_update() on {self.model.__name__} received object(s) whose "
                    f"tenant_field '{tenant_field}' still has an unresolved async default= "
                    f"value: pk(s) {pending_pks}. Save or refresh these objects first, or set "
                    f"'{tenant_field}' explicitly, before calling bulk_update()."
                )
        is_composite_pk = self.model._meta.has_composite_primary_key
        pk_values = [obj.pk for obj in objects]
        if (
            any(any(part is None for part in pk) for pk in pk_values)
            if is_composite_pk
            else any(map(is_, pk_values, repeat(None)))
        ):
            raise QueryError("All bulk_update() objects must have a primary key set.")
        # Two objects with one primary key would be two VALUES rows for one row - which of them is
        # written is unspecified.
        duplicate_pks: list[Any] = []
        if len(set(pk_values)) != len(pk_values):
            pks_seen: set[Any] = set()
            for pk in pk_values:
                if pk in pks_seen and pk not in duplicate_pks:
                    duplicate_pks.append(pk)
                pks_seen.add(pk)
        if duplicate_pks:
            raise QueryError(
                f"bulk_update() on {self.model.__name__} received multiple objects with the "
                f"same primary key in a single call - each target row can only be updated once "
                f"per call: pk(s) {duplicate_pks}"
            )
        # The optimistic lock field is compared on every bulk_update() - it has to be loaded.
        if optimistic_lock_field := self.model._meta.optimistic_lock_field:
            missing_version = [obj.pk for obj in objects if not hasattr(obj, optimistic_lock_field)]
            if missing_version:
                raise IncompleteInstanceError(
                    f"{self.model.__name__} is a partial model, Meta.optimistic_lock_field '{optimistic_lock_field}' "
                    f"is not available - every bulk_update() needs to read it for the staleness check, "
                    f"even when only updating other fields: pk(s) {missing_version}"
                )
        if self.model._meta.soft_delete_field in fields:
            raise QueryError(
                f"Cannot set '{self.model._meta.soft_delete_field}' via bulk_update() - "
                "use .delete()/.restore() instead"
            )
        if self.model._meta.optimistic_lock_field in fields:
            raise QueryError(
                f"Cannot set '{self.model._meta.optimistic_lock_field}' via bulk_update() - it's bumped automatically"
            )
        # A generated field can't be written.
        generated_fields = [
            field for field in fields if getattr(self.model._meta.fields_map.get(field), "generated", False)
        ]
        if generated_fields:
            raise QueryError(
                f"bulk_update() on {self.model.__name__} can't target generated field(s) "
                f"{generated_fields} - they're computed by the database, not written to."
            )
        # A forward relation is written through its key column(s); a reverse or many-to-many
        # relation has no column on this table.
        unsupported_fields = [
            field
            for field in fields
            if isinstance(
                self.model._meta.fields_map.get(field),
                (ManyToManyFieldInstance, BackwardFKRelation, BackwardOneToOneRelation),
            )
        ]
        if unsupported_fields:
            raise QueryError(
                f"bulk_update() doesn't support relation field(s) {unsupported_fields} - they have no "
                "column on this model's own table to update."
            )
        # Every object of a statement writes the same columns - a field still holding
        # DatabaseDefault can't be left out for one object. A field waiting for an async default has
        # no value yet; it is resolved before the write.
        unset_db_default_fields = {
            field
            for obj in objects
            for field in fields
            if field not in obj._await_when_save and isinstance(getattr(obj, field), DatabaseDefault)
        }
        if unset_db_default_fields:
            raise QueryError(
                f"bulk_update() on {self.model.__name__} received object(s) whose {sorted(unset_db_default_fields)} "
                "field(s) still hold their DatabaseDefault sentinel (never populated, e.g. via "
                "bulk_create(returning=False)) - give them a real value first, or exclude the field(s) from "
                "bulk_update()'s own fields= argument."
            )
        # bulk_update() binds every value as a parameter - an expression can't be one.
        expression_fields = {
            field
            for obj in objects
            for field in fields
            if field not in obj._await_when_save and isinstance(getattr(obj, field), Expression)
        }
        if expression_fields:
            raise QueryError(
                f"bulk_update() on {self.model.__name__} doesn't support F()/expression values, but "
                f"{sorted(expression_fields)} hold one - use QuerySet.update() to apply an expression to "
                "rows matching a filter, or save() on each object."
            )
        # auto_now fields are always written, whatever `fields` names.
        for field_name, field_obj in self.model._meta.fields_map.items():
            if getattr(field_obj, "auto_now", False) and field_name not in fields:
                # An auto_now field is written on every bulk_update() - it has to be loaded.
                if not all(map(hasattr, objects, repeat(field_name))):
                    raise IncompleteInstanceError(
                        f"{self.model.__name__} is a partial model, auto_now field '{field_name}' is not "
                        "available - every bulk_update() needs to read and bump it, even when only "
                        "updating other fields"
                    )
                fields.append(field_name)
        from hare.query.statements.write.bulk_update_query import BulkUpdateQuery

        rows_queryset = self
        if self._uses_default_scope:
            # The objects are matched by their keys, like save() matches one: under the default
            # scope a soft-deleted object would match no row. Their tenant is checked above.
            rows_queryset = self._clone()
            rows_queryset._uses_default_scope = False
        bulk_update_query = BulkUpdateQuery(rows_queryset, objects, fields, batch_size=batch_size, returning=returning)
        bulk_update_query._tenant_scoped = tenant_scoped
        return bulk_update_query

    @CallsBeforeSetup.recorded_result
    def get_or_none(self, *args: Q, **kwargs: Any) -> QuerySetSingle[TRow | None]:
        """
        Fetch exactly one object matching the parameters.
        """
        if self._combination is not None:
            return cast(
                "QuerySetSingle[TRow | None]",
                self._get_combined_query()._get_single_queryset(
                    args, kwargs, exception=None, raise_does_not_exist=False
                ),
            )
        self._raise_if_sliced_values_rows_differ("get_or_none")
        return cast(
            "QuerySetSingle[TRow | None]",
            self._get_single_queryset(args, kwargs, exception=None, raise_does_not_exist=False),
        )

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
        self._raise_if_values_selected("create")
        self._raise_if_combined("create")
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
        await instance.save(using=self.get_connection(True), force_create=True)
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
        self._raise_if_values_selected("get_or_create")
        self._raise_if_combined("get_or_create")
        # The existence check reads on the connection the row would be created on: a replica may not
        # have the row yet.
        queryset = self._pinned_for_write()
        try:
            return await queryset._get_matching(kwargs), False
        except DoesNotExist:
            return await queryset._create_or_get(defaults or {}, kwargs)

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
        self._raise_if_values_selected("update_or_create")
        self._raise_if_combined("update_or_create")
        defaults = defaults or {}
        # Pinned to the write connection: the SELECT FOR UPDATE and the UPDATE after it must
        # land on one connection for the lock to mean anything.
        queryset = self._pinned_for_write()
        async with queryset._db._in_transaction() as connection:
            matching = queryset.using(connection).filter(**kwargs)
            if connection.features.supports_select_for_update:
                matching = matching.select_for_update()
            instance = cast("TModel | None", await matching.get_or_none())
            if instance is not None:
                # The lock field is refused only here, for an existing row - a created row may
                # set its initial version.
                await queryset._update_with_defaults(instance, defaults, connection)
                return instance, False
        instance, created = await queryset._create_or_get(
            defaults if create_defaults is None else create_defaults, kwargs
        )
        if not created and defaults:
            # A concurrent writer created the row first - this call's values still apply to it.
            await queryset._update_with_defaults(instance, defaults, queryset._db)
        return instance, created

    def _pinned_for_write(self) -> Self:
        """The queryset pinned to the connection its writes go to.

        Returns:
            The queryset itself when a connection is pinned already.
        """
        queryset = self
        if cast("DatabaseClient | None", self._db) is None:
            queryset = self._clone()
            queryset._apply_db(self.get_connection(True))
        return queryset

    def _get_matching(self, kwargs: dict[str, Any]) -> QuerySetSingle[TModel]:
        """The one row of the queryset matching ``kwargs`` - not ``get(**kwargs)``, whose own
        ``exception`` parameter would take a field of that name.

        Args:
            kwargs: The conditions.

        Returns:
            The awaitable single-row query.
        """
        return cast(
            "QuerySetSingle[TModel]",
            self._get_single_queryset((), kwargs, exception=None, raise_does_not_exist=True),
        )

    def _get_create_values(self, defaults: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
        """The field values of the object ``get_or_create()``/``update_or_create()`` creates:
        ``kwargs`` without its lookups (a lookup only filters, like Django), then ``defaults``.

        Args:
            defaults: The values for a created object.
            kwargs: The conditions.

        Returns:
            The field values.

        Raises:
            QueryError: ``defaults`` conflicts with an exact ``kwargs`` value.
        """
        exact_match_kwargs = {key: value for key, value in kwargs.items() if "__" not in key}
        for key in defaults.keys() & exact_match_kwargs.keys():
            if (default_value := defaults[key]) != (query_value := exact_match_kwargs[key]):
                raise QueryError(f"Conflict value with {key=}: {default_value=} vs {query_value=}")
        return {**exact_match_kwargs, **defaults}

    async def _create_or_get(self, defaults: dict[str, Any], kwargs: dict[str, Any]) -> tuple[TModel, bool]:
        """Creates the row ``kwargs`` describes on the pinned connection, or - when a concurrent writer
        created it first - reads that row. Inside a transaction, or for a model overriding
        ``save()``, the create runs in a savepoint or transaction, so a failed INSERT leaves the
        connection usable.

        Args:
            defaults: The values for a created object.
            kwargs: The conditions.

        Returns:
            The object and whether it was created.

        Raises:
            QueryError: ``defaults`` conflicts with an exact ``kwargs`` value.
            IntegrityError: The create failed for a reason other than the row already existing.
        """
        from hare.models import Model

        create_values = self._get_create_values(defaults, kwargs)
        db = self._db
        try:
            if isinstance(db, TransactionClient) or self.model.save is not Model.save:
                async with db._in_transaction() as connection:
                    return await self.using(connection).create(**create_values), True
            return await self.create(**create_values), True
        except IntegrityError as exc:
            try:
                return await self._get_matching(kwargs), False
            except DoesNotExist:
                raise exc from None

    async def _update_with_defaults(self, instance: TModel, defaults: dict[str, Any], db: DatabaseClient) -> None:
        """Writes ``update_or_create()``'s ``defaults`` onto the existing row.

        Args:
            instance: The existing row.
            defaults: The values to write.
            db: The connection to save on.

        Raises:
            QueryError: ``defaults`` sets ``Meta.optimistic_lock_field``.
            FieldError: ``defaults`` names a field the model doesn't have.
        """
        meta = self.model._meta
        if (optimistic_lock_field := meta.optimistic_lock_field) and optimistic_lock_field in defaults:
            raise QueryError(
                f"Cannot set '{optimistic_lock_field}' via update_or_create() - it's bumped automatically"
            )
        known_field_names = (
            {"pk"}
            | meta.fk_fields
            | meta.o2o_fields
            | set(meta.fields_db_projection)
            | meta.backward_fk_fields
            | meta.backward_o2o_fields
            | meta.m2m_fields
        )
        for field_name in defaults:
            if field_name not in known_field_names:
                raise FieldError(f"Unknown field '{field_name}' for model {meta.full_name}")
        await instance.update_from_dict(defaults).save(using=db)

    @CallsBeforeSetup.recorded
    def only(self, *fields_for_select: str) -> Self:
        """Fetches only the given fields, creating partial model instances. Reading a field left out
        raises ``AttributeError``; ``save()`` of a partial instance needs ``update_fields`` naming
        loaded fields and the primary key loaded, else it raises ``IncompleteInstanceError``.

        Raises:
            ValueError: No field names are given, or ``.defer()`` was already used.
        """
        self._raise_if_combined("only")
        self._raise_if_values_selected("only")
        self._check_selected_names(fields_for_select, "only")
        if not fields_for_select:
            raise QueryError(".only() requires at least one field")
        if self._deferred_fields:
            raise QueryError(".only() cannot be combined with .defer() on the same queryset")
        queryset = self._clone()
        # "pk" and a forward relation name load their own key field(s), like Django - a to-many
        # relation name is left as given.
        only_field_names: list[str] = []
        for field_name in fields_for_select:
            concrete_field_paths = self.get_concrete_field_paths(self.model, field_name)
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
        self._raise_if_combined("defer")
        self._raise_if_values_selected("defer")
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
        db = Connections.get(using) if isinstance(using, str) else using
        queryset = self._clone()
        queryset._apply_db(db if db else queryset._db)
        if db:
            queryset._db_explicitly_chosen = True
        return queryset

    def _set_instance_connection(self, instance: Model) -> None:
        """Makes this queryset fall back to the connection ``instance`` was loaded from or saved to
        when the router has no opinion. The queried model keeps its own choice when the instance
        never touched the database, when its default connection differs from the instance model's,
        or when the router routes the instance's model.

        Args:
            instance: The model instance the relation is read from.
        """
        if (
            instance._db_connection_name is not None
            and self.model._meta.default_connection == type(instance)._meta.default_connection
            and not type(instance)._is_routed()
        ):
            self._instance_connection_name = instance._db_connection_name

    def _share_ambient_scope(self, query: TDerivedQuery) -> TDerivedQuery:
        """Hands this queryset's default scope (``.all_tenants()``/``.include_deleted()`` state
        included) to a derived (count/update/delete/...) query, together with the connection of
        the model instance a related manager built it from.

        Args:
            query: The query just built from this queryset.

        Returns:
            ``query`` itself, with the ambient scope set.
        """
        query._instance_connection_name = self._instance_connection_name
        return self._share_default_scope(query)

    def __await__(self) -> Generator[Any, None, list[TRow]]:
        direct_get = self._direct_get
        if direct_get is not None:
            return self._await_direct_get(direct_get)
        db = cast("DatabaseClient | None", self._db)
        # A joined relation's rows are read by the query's own layout.
        if self._call_signature is not None and self._selection is None and not self._select_related:
            # The connection is chosen once - the query run as usual takes it.
            db = db or self.get_connection()
            awaitable = self._await_call_signature_rows(db)
            if awaitable is not None:
                return awaitable
        query = self._get_select_query()
        if db is not None and cast("DatabaseClient | None", query._db) is None:
            query._apply_db(db)
        if self._combination is None:
            self._hand_call_signature(query)
        # Made for this run alone - built and run as it is, not copied first.
        query._is_execution_query = True
        return query.__await__()

    async def __aiter__(self) -> AsyncIterator[TRow]:
        for val in await self:
            yield val

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
        return RowsQuery.iterate_query(self._get_select_query, chunk_size)

    def stream(self, chunk_size: int = 1000) -> AsyncIterator[TRow]:
        """Streams the rows off one server-side cursor (PostgreSQL) - a single consistent snapshot for
        the whole scan, unlike ``iterator()``, which runs a SELECT per page. Requires an open
        ``Transactions.atomic()`` block. Iterating raises:

        - QueryError: ``chunk_size`` isn't a positive integer, the call is outside a transaction,
          a ``before_cursor()`` query, or model instances with ``prefetch_related()``.
        - UnSupportedError: The database has no server-side streaming (SQLite).

        Args:
            chunk_size: How many rows to fetch per round trip.

        Returns:
            The rows, as an async iterator.
        """
        return RowsQuery.stream_query(self._get_select_query, chunk_size)

    @CallsBeforeSetup.recorded
    def select_related(self, *args: str | Select) -> Self:
        """Returns a queryset that also selects the given forward relations with a JOIN. Pass
        ``Select(relation, extra_condition=Q(...))`` instead of a name to condition that relation's
        JOIN itself.
        """
        self._raise_if_combined("select_related")
        self._raise_if_values_selected("select_related")

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
        if self._combination is not None:
            return cast("Self", self._get_combined_query()._get_prefetching_queryset(args))
        self._raise_if_values_selected("prefetch_related")
        queryset = self._clone()
        Prefetch.add_lookups(self.model, queryset._prefetch_map, queryset._prefetch_queries, args)
        return queryset

    @CallsBeforeSetup.recorded
    def defer_related(self, *fields: str) -> Self:
        """Opts the given relations out of their field-level ``lazy="joined"``/``"select"`` default for
        this query.
        """
        self._raise_if_combined("defer_related")
        self._raise_if_values_selected("defer_related")
        queryset = self._clone()
        queryset._deferred_related_fields = queryset._deferred_related_fields | set(fields)
        return queryset
