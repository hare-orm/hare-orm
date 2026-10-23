from __future__ import annotations

from collections.abc import Iterable

from hare.migrations.execution.executor.migration_executor import MigrationExecutor


async def plan_migrations(executor: MigrationExecutor, targets: Iterable[object] | None = None) -> list:
    """The steps migrating the executor's connection to the targets - every app's latest by default."""
    await executor.build_graph()
    return executor.get_migration_plan(targets, set(executor.loader.applied_migrations), executor.loader.graph)
