from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hare.core.config import HareConfig
from hare.migrations.api.migration_session import MigrationSession
from hare.migrations.execution.connection_migration_plan import ConnectionMigrationPlan
from hare.migrations.execution.executor.plan_step import PlanStep


async def migrate(
    *,
    config: Mapping[str, Any] | HareConfig | str,
    app_labels: Sequence[str] | None = None,
    target: str | None = None,
    fake: bool = False,
    dry_run: bool = False,
    reporter: Callable[[str, list[PlanStep], bool, bool], object] | None = None,
    progress: Callable[[str, str, str], object] | None = None,
    lock_timeout: float | None = None,
) -> list[ConnectionMigrationPlan]:
    """Applies and unapplies the migrations of the configured apps to reach ``target`` - every
    app's latest without it; a target before the applied migrations unapplies back to it,
    ``app.zero`` unapplies every migration of the app. ``lock_timeout`` - seconds a migration's
    statement may wait for a lock another session holds before the migration fails - takes
    precedence over the config's ``migrations.lock_timeout``.

    Returns:
        Structured shared-schema runs reported under each connection's lock; a dry run
        returns planned steps. The temporary session closes its clients before returning.
        Use ``MigrationSession`` to retain clients across several requests, including an
        in-memory database.
    """
    async with MigrationSession(config, app_labels) as session:
        return await session.migrate(
            target, fake=fake, dry_run=dry_run, reporter=reporter, progress=progress, lock_timeout=lock_timeout
        )
