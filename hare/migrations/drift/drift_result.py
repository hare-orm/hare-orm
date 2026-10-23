from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

from hare.migrations.drift.column_mismatch import ColumnMismatch
from hare.migrations.operations import HareOperation


@dataclass
class DriftResult:
    """The result of comparing a live database schema against the current models."""

    operations: list[HareOperation]
    untracked_tables: list[str]
    untracked_columns: list[tuple[str, str, str, str]]
    #: Columns no operation reconciles - a primary key the table declares otherwise than the model,
    #: and a foreign key of a ``swappable()`` relation still referencing the table it named before.
    mismatched_columns: list[ColumnMismatch] = dataclass_field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.operations or self.untracked_tables or self.untracked_columns or self.mismatched_columns)
