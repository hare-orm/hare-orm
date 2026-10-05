from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.query.queryset.relations.related_queryset.related_query_set import RelatedQuerySet


class RelationRows:
    """What an instance keeps of one of its to-many relations (``obj._<relation>``): whether the rows
    were fetched, the rows, and the relation object made for them. The relation object of fetched rows
    is held weakly, as long as something else holds it - the rows themselves refer to no instance, so an
    instance whose fetched relations were read (``prefetch_related()``) makes no reference cycle with
    them and is freed as soon as nothing refers to it, without the garbage collector. The relation
    object of rows not fetched is held - it is queried through, again and again."""

    __slots__ = ("_fetched", "related_objects", "held_relation", "relation_reference", "bound_settings")

    def __init__(self) -> None:
        self._fetched = False
        self.related_objects: list[Any] = []
        #: The relation object made for rows not fetched - None before one is made.
        self.held_relation: RelatedQuerySet[Any] | None = None
        #: The relation object made for fetched rows, held weakly - None before one is made.
        self.relation_reference: weakref.ref[RelatedQuerySet[Any]] | None = None
        #: The query settings the last relation object made - ``(key values, connection alias of the
        #: instance, queryset)``, a relation object made again for the same values takes them over;
        #: None before any were made. The queryset refers to no instance.
        self.bound_settings: tuple[tuple[Any, ...], str | None, QuerySet[Any]] | None = None

    @classmethod
    def make_for_prefetch(cls, obj: Model) -> Self:
        """The rows of an instance that prefetching fills - the rows and the fetched flag set by the
        caller.

        Args:
            obj: The instance.

        Returns:
            The rows.
        """
        relation_rows = cls.__new__(cls)
        relation_rows.held_relation = None
        relation_rows.relation_reference = None
        relation_rows.bound_settings = None
        return relation_rows

    def __getstate__(self) -> dict[str, Any]:
        """The rows without the relation object and its query settings - made again when the relation
        is read."""
        return {"_fetched": self._fetched, "related_objects": self.related_objects}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self._fetched = state["_fetched"]
        self.related_objects = state["related_objects"]
        self.held_relation = None
        self.relation_reference = None
        self.bound_settings = None
