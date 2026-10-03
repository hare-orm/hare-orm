from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.writer.migration_writer import MigrationWriter


@dataclass
class MigrationChanges:
    """What ``makemigrations()`` found - the migrations to write, not written yet.

    Attributes:
        writers: One writer per new migration; empty when nothing changed.
        warnings: Possible renames it didn't recognize - an added and a removed field that may be
            one renamed field.
        data_loss_warnings: Changes that lose stored values - a generated field turned plain.
    """

    writers: list[MigrationWriter] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data_loss_warnings: list[str] = field(default_factory=list)
