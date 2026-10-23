from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.migrations.operations import HareOperation


@dataclass
class SplitMigration:
    """One new migration of an app as ``MigrationSplitter`` cuts it - before it has a name.

    Args:
        app_label: The app.
        operations: Its operations, in the order they run.
        split_dependencies: The positions, among the split migrations, of the new migrations it depends on.
        existing_dependencies: The ``(app, name)`` of the migrations on disk it depends on.
    """

    app_label: str
    operations: list[HareOperation] = field(default_factory=list)
    split_dependencies: set[int] = field(default_factory=set)
    existing_dependencies: set[tuple[str, str]] = field(default_factory=set)
