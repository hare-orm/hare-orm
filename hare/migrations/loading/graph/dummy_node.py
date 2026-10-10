from __future__ import annotations

from hare.migrations.exceptions import MigrationLoadError
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.graph.node import Node


class DummyNode(Node):
    def __init__(self, key: MigrationKey, origin: MigrationKey, error_message: str):
        super().__init__(key)
        self.origin = origin
        self.error_message = error_message

    def raise_error(self) -> None:
        raise MigrationLoadError(self.error_message)
