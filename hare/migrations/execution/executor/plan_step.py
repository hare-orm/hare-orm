from __future__ import annotations

from dataclasses import dataclass

from hare.migrations.migration import Migration


@dataclass(frozen=True)
class PlanStep:
    """A step of a migration plan: a migration applied, or unapplied when ``backward``."""

    migration: Migration
    backward: bool
