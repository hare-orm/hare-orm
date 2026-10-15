from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.caching.cache import Cache
from hare.exceptions import QueryError
from hare.query.queryset.constants import RELATION_CLASS_CACHE_SIZE
from hare.query.queryset.queryset import QuerySet
from hare.query.queryset.relations.many_to_many.through_rows import ThroughRows
from hare.query.queryset.specification_copying import SpecificationCopying

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation


class LinkedRowsQueries:
    """The queries of the rows a many-to-many relation to a model on another connection links an
    instance to: each first reads their keys off the through table, on the connection of the model
    declaring the relation, then runs on the related model's connection filtered by them. Mixed into
    the relation and into the querysets made from it."""

    __slots__ = ()

    #: Queryset class -> its class reading the linked rows.
    CLASSES_BY_QUERYSET_CLASS: ClassVar[Cache[type]] = Cache(
        RELATION_CLASS_CACHE_SIZE, holds_sql=False, keyed_by_model=False
    )

    @staticmethod
    def get_class_for(queryset_class: type[QuerySet[Any]]) -> type[QuerySet[Any]]:
        """The class of a queryset made from such a relation - ``queryset_class`` reading the linked
        rows, holding the relation in ``linked_relation``.

        Args:
            queryset_class: The related model manager's queryset class.

        Returns:
            The class.
        """
        linked_class = LinkedRowsQueries.CLASSES_BY_QUERYSET_CLASS.get((queryset_class,))
        if linked_class is None:
            linked_class = LinkedRowsQueries.CLASSES_BY_QUERYSET_CLASS[(queryset_class,)] = type(
                f"Linked{queryset_class.__name__}",
                (LinkedRowsQueries, queryset_class),
                {"__slots__": ("linked_relation",)},
            )
        return cast("type[QuerySet[Any]]", linked_class)

    def _get_linked_relation(self) -> ManyToManyRelation[Any]:
        """The relation whose links the queries read."""
        return cast("ManyToManyRelation[Any]", self.linked_relation)  # type: ignore[attr-defined]

    def _get_linked_queryset(self, keys: list[Any]) -> QuerySet[Any]:
        """This queryset as one of the related model manager's queryset class, filtered by the keys of
        the linked rows - before its slice, which then takes the linked rows.

        Args:
            keys: The keys of the linked rows.

        Returns:
            The queryset.
        """
        queryset = self._get_plain_queryset()
        limit, offset = queryset._limit, queryset._offset
        queryset._limit, queryset._offset = None, None
        queryset = queryset.filter(pk__in=keys)
        queryset._limit, queryset._offset = limit, offset
        return queryset

    def _get_plain_queryset(self) -> QuerySet[Any]:
        """This queryset as one of the related model manager's queryset class."""
        model = cast("QuerySet[Any]", self).model
        queryset_class = model._meta.manager.queryset_class
        queryset = queryset_class.__new__(queryset_class)
        SpecificationCopying.compile_copy(queryset_class)(self, queryset)
        queryset._prefetch_queries = {
            key: list(value) for key, value in cast("QuerySet[Any]", self)._prefetch_queries.items()
        }
        return queryset

    async def _run_on_linked_rows(self, call: Callable[[QuerySet[Any]], Awaitable[Any]]) -> Any:
        """Reads the keys of the linked rows, then runs ``call`` on this queryset filtered by them.

        Args:
            call: The query of the filtered queryset.

        Returns:
            What it returned.
        """
        keys = await ThroughRows.read_linked_keys(self._get_linked_relation())
        return await call(self._get_linked_queryset(keys))

    def __await__(self) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset).__await__()

    async def __aiter__(self) -> AsyncIterator[Any]:
        for row in await self:
            yield row

    async def iterator(self, chunk_size: int = 1000) -> AsyncIterator[Any]:
        keys = await ThroughRows.read_linked_keys(self._get_linked_relation())
        async for row in self._get_linked_queryset(keys).iterator(chunk_size):
            yield row

    async def stream(self, chunk_size: int = 1000) -> AsyncIterator[Any]:
        keys = await ThroughRows.read_linked_keys(self._get_linked_relation())
        async for row in self._get_linked_queryset(keys).stream(chunk_size):
            yield row

    def union(self, *other_querysets: Any, all: bool = False) -> Any:
        raise self._get_set_operation_error("union")

    def intersection(self, *other_querysets: Any) -> Any:
        raise self._get_set_operation_error("intersection")

    def difference(self, *other_querysets: Any) -> Any:
        raise self._get_set_operation_error("difference")

    def _get_set_operation_error(self, operation_name: str) -> QueryError:
        """The error of a set operation - its branches are read by the keys their links hold, which no
        single statement reads.

        Args:
            operation_name: The operation.

        Returns:
            The error.
        """
        return QueryError(
            f"{operation_name}() of a relation to a model on another connection isn't possible - its rows "
            "are read by the keys of its links first; combine the keys (values_list('pk', flat=True)) instead."
        )

    def count(self) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.count())

    def exists(self) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.exists())

    def aggregate(self, **kwargs: Any) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.aggregate(**kwargs))

    def contains(self, obj: Any) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.contains(obj))

    async def in_bulk(self, id_list: Any = None, *, field_name: str = "pk") -> Any:
        return await self._run_on_linked_rows(lambda queryset: queryset.in_bulk(id_list, field_name=field_name))

    def first(self) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.first())

    def last(self) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.last())

    def latest(self, *orderings: Any) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.latest(*orderings))

    def earliest(self, *orderings: Any) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.earliest(*orderings))

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.get(*args, **kwargs))

    def update(self, **kwargs: Any) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.update(**kwargs))

    def delete(self) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.delete())

    def hard_delete(self) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.hard_delete())

    def restore(self, *, cascade: bool = False) -> Any:
        return self._run_on_linked_rows(lambda queryset: queryset.restore(cascade=cascade))
