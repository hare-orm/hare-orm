from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hare.core.config import HareConfig
from hare.migrations.api.migration_session import MigrationSession
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder


async def sqlmigrate(
    *,
    config: Mapping[str, Any] | HareConfig | str,
    app_label: str,
    migration_name: str,
    backward: bool = False,
) -> list[str]:
    """Collect the SQL statements for a single migration without executing them.

    Args:
        config: The configuration, in any form ``HareConfig.load()`` takes.
        app_label: The application label.
        migration_name: The migration name (exact or prefix match).
        backward: If True, collect SQL for unapplying the migration.

    Returns:
        A list of SQL strings (including descriptive comment annotations).
    """
    async with MigrationSession(config, [app_label]) as session:
        executors = session.create_executors()
        executor = next(iter(executors.values()))
        # SQL collection uses definitions only and does not query an applied journal.
        executor.loader.recorder = NoopRecorder()
        return await executor.collect_sql(app_label, migration_name, backward=backward)
