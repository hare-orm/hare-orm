from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.writer.migration_writer import MigrationWriter


@dataclass
class SquashedMigration:
    """What ``squashmigrations()`` built - the migration to write, not written yet.

    Attributes:
        writer: The squashed migration - None when the range holds one migration.
        replaced_names: The migrations it replaces - the originals of a squashed one among them.
        elided_operation_count: The ``elidable`` ``RunPython``/``RunSQL`` operations left out.
        squashed_operation_count: The operations squashed, before they were shortened.
    """

    writer: MigrationWriter | None
    replaced_names: list[str] = field(default_factory=list)
    elided_operation_count: int = 0
    squashed_operation_count: int = 0
