from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.migrations.loading.graph.migration_key import MigrationKey
    from hare.migrations.loading.migration_loader import MigrationLoader
    from hare.migrations.migration import Migration


@dataclass(frozen=True, slots=True)
class MigrationDefinitions:
    """A session's definitions, independent of imported classes and database journals.

    Each loader receives independent declarations through the operations' snapshot protocol.
    Rebuilding its graph after
    acquiring a database lock refreshes journal state without importing different files
    halfway through a migration request.
    """

    migrations: Mapping[MigrationKey, Migration]
    migrated_apps: frozenset[str]
    unmigrated_apps: frozenset[str]

    @classmethod
    def capture(cls, loader: MigrationLoader) -> MigrationDefinitions:
        """Capture loaded declarations without retaining their mutable operation objects."""
        return cls(
            MappingProxyType(cls.snapshot_migrations(loader.disk_migrations)),
            frozenset(loader.migrated_apps),
            frozenset(loader.unmigrated_apps),
        )

    def copy_migrations(self) -> dict[MigrationKey, Migration]:
        """Give one graph its independent declaration objects."""
        return self.snapshot_migrations(self.migrations)

    @staticmethod
    def snapshot_migrations(migrations: Mapping[MigrationKey, Migration]) -> dict[MigrationKey, Migration]:
        """Copy migration metadata while letting each operation preserve its external resources.

        Shared top-level operation declarations keep their identity within the copied graph. Operations
        decide which declaration values need copying; callbacks are never invoked here.
        """
        memo: dict[int, Any] = {}
        for migration in migrations.values():
            for operation in migration.operations:
                if id(operation) not in memo:
                    memo[id(operation)] = operation.snapshot()
        return deepcopy(dict(migrations), memo)
