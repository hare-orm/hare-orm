from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Iterable, Iterator
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar, cast, overload

from hare.core.cache import Cache
from hare.exceptions import (
    NoValuesFetched,
)
from hare.query.constants import RELATION_CLASS_CACHE_SIZE
from hare.query.queryset.queryset import QuerySet

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
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
        instance: The instance the relation is read from.
        from_fields: The instance's fields holding the values ``relation_fields`` must equal.
    """

    __slots__ = (
        "instance",
        "relation_fields",
        "from_fields",
        "related_objects",
        "_fetched",
        "_bound_key_values",
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
        instance: Model,
        from_fields: tuple[str, ...],
    ) -> None:
        # The query settings come from the related model's manager when the relation is read off
        # the instance - see _bind().
        self.model = remote_model
        self.relation_fields = relation_fields
        self.instance = instance
        self.from_fields = from_fields
        self._fetched = False
        self.related_objects: list[TModel] = []
        #: The instance's key values the query settings were made for.
        self._bound_key_values: tuple[Any, ...] | None = None

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
        for name, value in state.items():
            setattr(self, name, value)
        self._bound_key_values = None

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
            if self._fetched:
                # The rows are here already - a referenced field left unloaded matches no row
                # when queried.
                key_values = tuple(getattr(instance, field_name, None) for field_name in self.from_fields)
            else:
                key_values = tuple(
                    instance._get_relation_key_values(self.from_fields, f"The relation to {self.model.__name__}")
                )
            if key_values == self._bound_key_values and not manager.overrides_get_queryset:
                return
            queryset = manager.get_queryset()
            # _append_filters(), the conditions built only once the relation is queried - a
            # relation whose fetched rows are only read never builds them.
            queryset._call_signature = None
            queryset._filter_call_counter += 1
            queryset._pending_filter_calls = (
                *queryset._pending_filter_calls,
                (False, queryset._filter_call_counter, dict(zip(self.relation_fields, key_values, strict=True))),
            )
            queryset._set_instance_connection(instance)
            # A NULL key (a nullable to_field=) is referenced by no row.
            queryset._is_none = None in key_values
        type(queryset)._compiled_copy()(queryset, self)
        if hasattr(queryset, "__dict__"):
            self.__dict__.update(queryset.__dict__)
        self._bound_key_values = key_values

    def _clone(self) -> Self:
        # A queryset of the manager's class - without the relation's writes and fetched rows.
        queryset_class = self.model._meta.manager.queryset_class
        queryset = queryset_class.__new__(queryset_class)
        queryset_class._compiled_copy()(self, queryset)
        queryset._prefetch_queries = {key: list(value) for key, value in self._prefetch_queries.items()}
        return cast("Self", queryset)

    def _get_model_queryset(self, db: DatabaseClient) -> QuerySet[TModel]:
        """A queryset of the related model's manager pinned to ``db`` - what the relation's writes
        create and match rows of the related model through.

        Args:
            db: The connection.

        Returns:
            The queryset.
        """
        queryset = cast("QuerySet[TModel]", self.model._meta.manager.get_queryset())
        queryset._apply_db(db)
        queryset._db_explicitly_chosen = True
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

    async def __aiter__(self) -> AsyncGenerator[TModel]:
        if not self._fetched:
            self._set_result_for_query(await self)
        for row in self.related_objects:
            yield row

    def _get_fetched_rows(self) -> list[TModel]:
        """The relation's rows as fetched.

        Raises:
            NoValuesFetched: The rows weren't fetched.
        """
        if not self._fetched:
            raise NoValuesFetched("No values were fetched for this relation - await it or prefetch it")
        return self.related_objects

    def _set_result_for_query(self, sequence: list[TModel], attr: str | None = None) -> None:
        # Several Prefetch(..., to_attr=...) of one relation share this container - a to_attr result
        # leaves the bare relation unfetched.
        if attr:
            setattr(self.instance, attr, sequence)
        else:
            self._fetched = True
            self.related_objects = sequence

    def _raise_if_not_fetched(self) -> None:
        self._get_fetched_rows()

    def _invalidate_local_cache(self) -> None:
        """Drops the relation's fetched rows after a write through it, so the next read reflects the
        write.
        """
        self._fetched = False
        self.related_objects = []

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
