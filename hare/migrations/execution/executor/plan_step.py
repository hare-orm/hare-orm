from __future__ import annotations

from dataclasses import dataclass

from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.migration import Migration


@dataclass(frozen=True)
class PlanStep:
    """A step of a migration plan: a migration applied, or unapplied when ``backward``."""

    migration: Migration
    backward: bool

    @property
    def key(self) -> MigrationKey:
        """The migration this step selects."""
        return MigrationKey(app_label=self.migration.app_label, name=self.migration.name)

    def update_applied(self, applied: set[MigrationKey]) -> None:
        """Project the effect of the step on the set of applied migrations."""
        if self.backward:
            applied.discard(self.key)
        else:
            applied.add(self.key)
