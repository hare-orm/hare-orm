from __future__ import annotations

import inspect
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar, cast, overload

from hare.core.caching.cache import Cache
from hare.exceptions import (
    NoValuesFetched,
)
from hare.fields.relations.relation_values import RelationValues
from hare.query.expressions.constants import PLAIN_VALUE_TYPES
from hare.query.queryset.arguments.filter_arguments import FilterArguments
from hare.query.queryset.constants import RELATION_CLASS_CACHE_SIZE
from hare.query.queryset.queryset import QuerySet
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
from hare.query.queryset.relations.related_queryset.related_rows_iterator import RelatedRowsIterator
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows
from hare.query.queryset.relations.related_queryset.unsaved_owner_condition import UnsavedOwnerCondition

TModel = TypeVar("TModel", bound="Model")


class RelatedQuerySet(QuerySet[TModel, TModel]):
    """The rows a to-many relation of one instance points at: the related model's queryset filtered to
    them, read off the instance (``tournament.events``). A method changing which rows it returns
    gives a plain queryset. It also holds the rows once fetched, and is then iterated, measured and
    indexed like a list.

    Args:
        remote_model: The related model.
        relation_fields: The related model's fields the rows are filtered by.
        obj: The instance the relation is read from.
        from_fields: The instance's fields holding the values ``relation_fields`` must equal.
    """

    __slots__ = (
        "instance",
        "relation_fields",
        "from_fields",
        "relation_rows",
        "_bound_key_values",
        "_is_bound",
        # The obj's RelationRows holds its relation object weakly.
        "__weakref__",
    )

    #: (relation class, queryset class) -> the relation class of related models whose manager has
    #: a queryset class of its own - such a relation has that class's methods too.
    CLASSES_BY_QUERYSET_CLASS: ClassVar[Cache[type]] = Cache(
        RELATION_CLASS_CACHE_SIZE, holds_sql=False, keyed_by_model=False
    )

    def __init__(
        self,
        remote_model: type[TModel],
        relation_fields: tuple[str, ...],
        obj: Model,
        from_fields: tuple[str, ...],
        relation_rows: RelationRows | None = None,
    ) -> None:
        # The query settings come from the related model's manager when the relation is read off
        # the obj - see _bind().
        self.model = remote_model
        self.relation_fields = relation_fields
        self.instance = obj
        self.from_fields = from_fields
        #: The fetched rows - the ones the obj keeps, when given.
        self.relation_rows = RelationRows() if relation_rows is None else relation_rows
        #: The obj's key values the query settings were made for.
        self._bound_key_values: tuple[Any, ...] | None = None
        #: Whether the query settings were made - a relation read off the obj with its rows
        #: fetched makes them on their first use.
        self._is_bound = False

    @classmethod
    def get_class_for(cls, remote_model: type[Model]) -> type[Self]:
        """The class of this relation to ``remote_model`` - with the methods of the queryset
        class its manager declares.

        Args:
            remote_model: The related model.

        Returns:
            The class.
        """
        queryset_class = remote_model._meta.manager.queryset_class
        if queryset_class is QuerySet:
            return cls
        key = (cls, queryset_class)
        relation_class = RelatedQuerySet.CLASSES_BY_QUERYSET_CLASS.get(key)
        if relation_class is None:
            relation_class = RelatedQuerySet.CLASSES_BY_QUERYSET_CLASS[key] = type(
                f"{cls.__name__}{queryset_class.__name__}", (cls, queryset_class), {"__slots__": ()}
            )
        return cast("type[Self]", relation_class)

    def __getstate__(self) -> dict[str, Any]:
        """The relation without its query settings - they hold connections, and are made again
        when the relation is next read."""
        return {
            "model": self.model,
            "relation_fields": self.relation_fields,
            "instance": self.instance,
            "from_fields": self.from_fields,
            "_fetched": self._fetched,
            "related_objects": self.related_objects,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.relation_rows = RelationRows()
        for name, value in state.items():
            setattr(self, name, value)
        self._bound_key_values = None
        self._is_bound = False

    @property
    def related_objects(self) -> list[TModel]:
        """The fetched rows."""
        return self.relation_rows.related_objects

    @related_objects.setter
    def related_objects(self, rows: list[TModel]) -> None:
        self.relation_rows.related_objects = rows

    @property
    def _fetched(self) -> bool:
        """Whether the rows were fetched."""
        return self.relation_rows._fetched

    @_fetched.setter
    def _fetched(self, fetched: bool) -> None:
        self.relation_rows._fetched = fetched

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any:
            # A query setting of a fetched relation not bound yet - made now.
            if not self._is_bound:
                self._bind()
                try:
                    return object.__getattribute__(self, name)
                except AttributeError:
                    pass
            return super().__getattr__(name)

    def _bind(self) -> None:
        """Makes the query settings for the instance's current key values - each time the relation is
        read off the instance. The relation of an unsaved instance holds the rows given for it;
        querying through it is refused when the query runs.

        Raises:
            QueryError: A field the relation references the instance by was left unloaded.
        """
        instance = self.instance
        manager = self.model._meta.manager
        if not instance._saved_in_db:
            queryset = manager.get_queryset()
            queryset._q_objects.append(UnsavedOwnerCondition())
            key_values = None
        else:
            if self.relation_rows._fetched:
                # The rows are here already - a referenced field left unloaded matches no row
                # when queried.
                key_values = tuple([getattr(instance, field_name, None) for field_name in self.from_fields])
            else:
                try:
                    key_values = tuple([getattr(instance, field_name) for field_name in self.from_fields])
                except AttributeError:
                    # A referenced field left unloaded - the error names it.
                    RelationValues.get_relation_key_values(
                        instance, self.from_fields, f"The relation to {self.model.__name__}"
                    )
                    raise
            if key_values == self._bound_key_values and not manager.overrides_get_queryset:
                return
            relation_rows = self.relation_rows
            bound_settings = relation_rows.bound_settings
            if (
                bound_settings is not None
                and bound_settings[0] == key_values
                and bound_settings[1] == instance._connection_alias
                and not manager.overrides_get_queryset
            ):
                # A relation object made again for the obj's rows - the settings the last one made
                # for these key values.
                self._copy_bound_settings(bound_settings[2], key_values)
                return
            queryset = manager.get_queryset()
            # _append_filters(), the conditions built only once the relation is queried - a
            # relation whose fetched rows are only read never builds them.
            relation_filters = dict(zip(self.relation_fields, key_values, strict=True))
            queryset._filter_call_counter += 1
            queryset._pending_filter_calls = (
                *queryset._pending_filter_calls,
                (False, queryset._filter_call_counter, (), relation_filters),
            )
            # Its queries run by the calls made on it, the relation's filter first - as a filter()
            # of plain values on the manager's queryset (QuerySet._call_signature).
            if (
                not manager.overrides_get_queryset
                and queryset._visibility is RowVisibility.DEFAULT
                and all(type(value) in PLAIN_VALUE_TYPES for value in key_values)
                and FilterArguments.takes_pending_filters(queryset, relation_filters)
            ):
                queryset._call_signature = (type(queryset), ("filter", self.relation_fields))
                queryset._call_values = key_values
            else:
                queryset._call_signature = None
            queryset._set_instance_connection(instance)
            # A NULL key (a nullable to_field=) is referenced by no row.
            queryset._is_none = None in key_values
            if not manager.overrides_get_queryset:
                relation_rows.bound_settings = (key_values, instance._connection_alias, queryset)
        self._copy_bound_settings(queryset, key_values)

    def _copy_bound_settings(self, queryset: QuerySet[TModel], key_values: tuple[Any, ...] | None) -> None:
        """Takes over the query settings made for the obj's key values.

        Args:
            queryset: The queryset holding them.
            key_values: The key values they were made for; None for an unsaved obj.
        """
        SpecificationCopying.compile_copy(type(queryset))(queryset, self)
        if hasattr(queryset, "__dict__"):
            self.__dict__.update(queryset.__dict__)
        self._bound_key_values = key_values
        self._is_bound = True

    def _clone(self) -> Self:
        # A queryset of the manager's class - without the relation's writes and fetched rows.
        queryset_class = self.model._meta.manager.queryset_class
        queryset = queryset_class.__new__(queryset_class)
        SpecificationCopying.compile_copy(queryset_class)(self, queryset)
        queryset._prefetch_queries = {key: list(value) for key, value in self._prefetch_queries.items()}
        return queryset  # type: ignore[return-value]

    def _get_model_queryset(self, connection: DatabaseClient) -> QuerySet[TModel]:
        """A queryset of the related model's manager pinned to ``connection`` - what the relation's writes
        create and match rows of the related model through.

        Args:
            connection: The connection.

        Returns:
            The queryset.
        """
        queryset = cast("QuerySet[TModel]", self.model._meta.manager.get_queryset())
        queryset._apply_connection(connection)
        queryset._connection_explicitly_chosen = True
        return queryset

    def __contains__(self, item: Any) -> bool:
        return item in self._get_fetched_rows()

    def __iter__(self) -> Iterator[TModel]:
        return self._get_fetched_rows().__iter__()

    def __len__(self) -> int:
        return len(self._get_fetched_rows())

    def __bool__(self) -> bool:
        return bool(self._get_fetched_rows())

    @overload  # type: ignore[override]
    def __getitem__(self, key: slice) -> Self: ...

    @overload
    def __getitem__(self, key: int) -> TModel: ...

    def __getitem__(self, key: slice | int) -> Self | TModel:
        """A fetched row by its position, or the queryset of a slice of the relation's rows."""
        if isinstance(key, slice):
            return super().__getitem__(key)
        return self._get_fetched_rows()[key]

    def __aiter__(self) -> RelatedRowsIterator[TModel]:
        return RelatedRowsIterator(self)

    def _get_fetched_rows(self) -> list[TModel]:
        """The relation's rows as fetched.

        Raises:
            NoValuesFetched: The rows weren't fetched.
        """
        relation_rows = self.relation_rows
        if not relation_rows._fetched:
            raise NoValuesFetched("No values were fetched for this relation - await it or prefetch it")
        return relation_rows.related_objects

    def _set_result_for_query(self, sequence: list[TModel], attribute_name: str | None = None) -> None:
        # Several Prefetch(..., to_attribute=...) of one relation share this container - a to_attribute result
        # leaves the bare relation unfetched.
        if attribute_name:
            setattr(self.instance, attribute_name, sequence)
        else:
            relation_rows = self.relation_rows
            relation_rows._fetched = True
            relation_rows.related_objects = sequence

    def _invalidate_local_cache(self) -> None:
        """Drops the relation's fetched rows after a write through it, so the next read reflects the
        write.
        """
        relation_rows = self.relation_rows
        relation_rows._fetched = False
        relation_rows.related_objects = []

    @staticmethod
    async def _get_set_members(members: tuple[Any, ...]) -> tuple[Any, ...]:
        """The members a ``set()`` call names - its arguments, or the elements of its one iterable
        or awaitable (a list, a queryset, another relation) argument, like Django.

        Args:
            members: ``set()``'s positional arguments.

        Returns:
            The members.
        """
        from hare.models import Model

        if len(members) != 1 or isinstance(members[0], (Model, str, bytes)):
            return members
        only_member = members[0]
        if inspect.isawaitable(only_member):
            return tuple(await only_member)
        if isinstance(only_member, Iterable):
            return tuple(only_member)
        return members
