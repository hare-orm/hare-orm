from __future__ import annotations

from functools import total_ordering
from typing import TYPE_CHECKING

from hare.migrations.loading.graph.migration_key import MigrationKey

if TYPE_CHECKING:
    from hare.migrations.migration import Migration


@total_ordering
class Node:
    """A migration in the graph, with the ones it depends on and the ones depending on it."""

    def __init__(self, key: MigrationKey, migration: Migration | None = None) -> None:
        self.key = key
        self.migration = migration
        self.children: set[Node] = set()
        self.parents: set[Node] = set()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Node):
            return self.key == other.key
        return self.key == other

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Node):
            return self.key < other.key
        return self.key < other  # type: ignore[operator]

    def __hash__(self) -> int:
        return hash(self.key)

    def __str__(self) -> str:
        return str(self.key)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: ({self.key.app_label!r}, {self.key.name!r})>"

    def add_parent(self, parent: Node) -> None:
        self.parents.add(parent)
        parent.children.add(self)
