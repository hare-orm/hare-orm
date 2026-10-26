from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from hare.ddl.schema_objects.view import View

TItem = TypeVar("TItem")


class ViewCreationOrder:
    """The order views and materialized views are created in - each after the ones its ``read_columns``
    name: a view reading a materialized view after it, and the other way round. Raw SQL is never read for
    it - an object naming none keeps its place."""

    @staticmethod
    def get_ordered(items: Sequence[TItem], get_view: Callable[[TItem], View]) -> list[TItem]:
        """Items holding views, each after the ones whose views it reads.

        Args:
            items: The items - views, or the operations creating them - in their order by type.
            get_view: The view of an item.

        Returns:
            The items; a cycle keeps the given order.
        """
        names = {get_view(item).name for item in items}
        pending = list(items)
        ordered: list[TItem] = []
        created_names: set[str] = set()
        while pending:
            next_item = next(
                (
                    item
                    for item in pending
                    if {table for table, _column in get_view(item).read_columns if table in names}
                    - {get_view(item).name}
                    <= created_names
                ),
                pending[0],
            )
            pending.remove(next_item)
            ordered.append(next_item)
            created_names.add(get_view(next_item).name)
        return ordered
