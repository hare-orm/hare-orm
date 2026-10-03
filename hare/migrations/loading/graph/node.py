from __future__ import annotations

from functools import total_ordering

from hare.migrations.loading.graph.migration_key import MigrationKey


@total_ordering
class Node:
    def __init__(self, key: MigrationKey):
        self.key = key
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

    def __getitem__(self, item: int) -> str:
        return (self.key.app_label, self.key.name)[item]

    def __str__(self) -> str:
        return str(self.key)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: ({self.key.app_label!r}, {self.key.name!r})>"

    def add_child(self, child: Node) -> None:
        self.children.add(child)

    def add_parent(self, parent: Node) -> None:
        self.parents.add(parent)
