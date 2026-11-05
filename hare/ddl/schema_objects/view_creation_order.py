from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from hare.ddl.schema_objects.view import View

TItem = TypeVar("TItem")


class ViewCreationOrder:
    """Order views by declared and queryset-derived dependencies, without parsing raw SQL."""

    @staticmethod
    def get_ordered(items: Sequence[TItem], get_view: Callable[[TItem], View]) -> list[TItem]:
        """Items holding views, each after the ones whose views it reads.

        Args:
            items: The items - views, or the operations creating them - in their order by type.
            get_view: The view of an item.

        Returns:
            The items; a cycle keeps the given order.
        """
        views = [get_view(item) for item in items]
        names = {view.name for view in views}
        dependencies = {
            view.name: {table for table, _column in view.get_read_columns() if table in names} - {view.name}
            for view in views
        }
        pending = list(items)
        ordered: list[TItem] = []
        created_names: set[str] = set()
        while pending:
            next_item = next(
                (item for item in pending if dependencies[get_view(item).name] <= created_names),
                pending[0],
            )
            pending.remove(next_item)
            ordered.append(next_item)
            created_names.add(get_view(next_item).name)
        return ordered
