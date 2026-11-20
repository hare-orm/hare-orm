from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hare.core.config import HareConfig
from hare.migrations.api.migration_session import MigrationSession
from hare.migrations.execution.connection_migration_plan import ConnectionMigrationPlan


async def plan(
    *,
    config: Mapping[str, Any] | HareConfig | str,
    app_labels: Sequence[str] | None = None,
    target: str | None = None,
) -> list[ConnectionMigrationPlan]:
    """Return ordered connection/step records without printing or changing the caller's context."""
    async with MigrationSession(config, app_labels) as session:
        return await session.plan(target)
