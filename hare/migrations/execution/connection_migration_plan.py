from __future__ import annotations

from dataclasses import dataclass

from hare.migrations.execution.executor.plan_step import PlanStep


@dataclass(frozen=True, slots=True)
class ConnectionMigrationPlan:
    """An ordered run of migration steps for one connection; no presentation text."""

    connection_alias: str
    steps: tuple[PlanStep, ...]
