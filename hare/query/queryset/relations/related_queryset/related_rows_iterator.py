from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset.relations.related_queryset.related_query_set import RelatedQuerySet

TModel = TypeVar("TModel", bound="Model")


class RelatedRowsIterator(Generic[TModel]):
    """``async for`` over a to-many relation's rows - fetched by the first step when they aren't yet.
    An object, not an async generator: the event loop keeps no record of it, and a relation of rows
    fetched ahead (``prefetch_related()``) is iterated without a query.
    """

    __slots__ = ("relation", "rows")

    def __init__(self, relation: RelatedQuerySet[TModel]) -> None:
        """
        Args:
            relation: The relation.
        """
        self.relation = relation
        self.rows: Iterator[TModel] | None = None

    def __aiter__(self) -> RelatedRowsIterator[TModel]:
        return self

    async def __anext__(self) -> TModel:
        rows = self.rows
        if rows is None:
            relation = self.relation
            relation_rows = relation.relation_rows
            if not relation_rows._fetched:
                relation._set_result_for_query(await relation)
            rows = self.rows = iter(relation_rows.related_objects)
        for row in rows:
            return row
        raise StopAsyncIteration
