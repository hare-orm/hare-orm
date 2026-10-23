from __future__ import annotations

from hare.migrations.exceptions import MigrationLoadError
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.graph.node import Node


class DummyNode(Node):
    """A node a migration depends on that no migration file holds - the graph fails on it when it is
    checked."""

    def __init__(self, key: MigrationKey, error_message: str):
        super().__init__(key)
        self.error_message = error_message

    def raise_error(self) -> None:
        """Raises the node's error.

        Raises:
            MigrationLoadError: Always.
        """
        raise MigrationLoadError(self.error_message)
