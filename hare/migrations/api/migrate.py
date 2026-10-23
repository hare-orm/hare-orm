from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hare.core.config import HareConfig
from hare.core.connections.connections import Connections
from hare.migrations.api.migration_request_parser import MigrationRequestParser
from hare.migrations.execution.connections_plan import ConnectionsPlan
from hare.migrations.execution.executor.migration_executor import MigrationExecutor
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
) -> None:
    """Applies and unapplies the migrations of the configured apps to reach ``target`` - every
    app's latest without it; a target before the applied migrations unapplies back to it,
    ``app.zero`` unapplies every migration of the app. ``lock_timeout`` - seconds a migration's
    statement may wait for a lock another session holds before the migration fails - takes
    precedence over the config's ``migrations.lock_timeout``."""
    hare_config = HareConfig.load(config)
    if lock_timeout is None and hare_config.migrations is not None:
        lock_timeout = hare_config.migrations.lock_timeout
    config = hare_config.to_dict()
    configured_apps, selected_apps, apps_by_connection = await MigrationRequestParser.init_selected_apps(
        config, app_labels
    )

    targets = MigrationRequestParser.parse_targets(target, selected_apps)
    # Every configured app is loaded by each executor: a dependency may name another connection's
    # migration. One plan over them all orders the migrations of every connection.
    executors = {
        connection_alias: MigrationExecutor(
            Connections.get(connection_alias), subset, full_apps_config=configured_apps, lock_timeout=lock_timeout
        )
        for connection_alias, subset in apps_by_connection.items()
    }
    await ConnectionsPlan(executors).migrate(targets, fake=fake, dry_run=dry_run, reporter=reporter, progress=progress)
