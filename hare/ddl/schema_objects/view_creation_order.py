from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, TypeVar

from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.ddl.schema_objects.view import View
    from hare.dialects.base.client.database_client import DatabaseClient

TItem = TypeVar("TItem")


class ViewCreationOrder:
    """Order views by declared and queryset-derived dependencies, without parsing raw SQL."""

    @staticmethod
    def get_affected_sources[Key, Source](
        dependencies: dict[Key, set[Key]], sources: dict[Key, set[Source]]
    ) -> dict[Key, set[Source]]:
        """Propagate material changes from tables and views to each dependent view."""
        while True:
            expanded = {
                key: causes.union(*(sources[dependency] for dependency in dependencies[key] if dependency in sources))
                for key, causes in sources.items()
            }
            if expanded == sources:
                return sources
            sources = expanded

    @staticmethod
    def get_ordered(
        items: Sequence[TItem],
        get_view: Callable[[TItem], View],
        *,
        get_schema: Callable[[TItem], str | None] | None = None,
        client: DatabaseClient | None = None,
    ) -> list[TItem]:
        """Items holding views, each after the ones whose views it reads.

        Args:
            items: The items - views, or the operations creating them - in their order by type.
            get_view: The view of an item.
            get_schema: The model's schema for each item, None for a single default schema.
            client: Explicit schema compilation resources for structural model queries.

        Returns:
            The items; a cycle keeps the given order.
        """
        views = [get_view(item) for item in items]
        keys = [
            (get_schema(item) if get_schema is not None else None, view.name)
            for item, view in zip(items, views, strict=True)
        ]
        view_keys = set(keys)
        dependencies = {
            key: {
                dependency
                for table, _column in view.get_read_columns(client)
                if (dependency := Identifiers.get_schema_and_name(table, key[0])) in view_keys and dependency != key
            }
            for key, view in zip(keys, views, strict=True)
        }
        pending = list(zip(items, keys, strict=True))
        ordered: list[TItem] = []
        created: set[tuple[str | None, str]] = set()
        while pending:
            next_entry = next(
                (entry for entry in pending if dependencies[entry[1]] <= created),
                pending[0],
            )
            pending.remove(next_entry)
            item, key = next_entry
            ordered.append(item)
            created.add(key)
        return ordered
