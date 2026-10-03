from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.writer.migration_writer import MigrationWriter


@dataclass
class SquashedMigration:
    """What ``squashmigrations()`` built - the migration to write, not written yet.

    Attributes:
        writer: The squashed migration - None when the app has only one migration.
        replaced_names: The migrations it replaces.
        data_migration_names: The replaced migrations running ``RunPython``/``RunSQL`` - their
            effect isn't in the squashed migration.
    """

    writer: MigrationWriter | None
    replaced_names: list[str] = field(default_factory=list)
    data_migration_names: list[str] = field(default_factory=list)
