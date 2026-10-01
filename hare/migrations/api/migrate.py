import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hare import Hare
from hare.core.config import HareConfig
from hare.core.connections import Connections
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.migrations.api.common import MigrationRequestParser
from hare.migrations.execution.executor.migration_executor import MigrationExecutor
from hare.migrations.execution.executor.plan_step import PlanStep
from hare.migrations.loading.migrations_modules import MigrationsModules


async def migrate(
    *,
    config: Mapping[str, Any] | HareConfig | str,
    app_labels: Sequence[str] | None = None,
    target: str | None = None,
    fake: bool = False,
    dry_run: bool = False,
    reporter: Callable[[str, list[PlanStep], bool, bool], object] | None = None,
    progress: Callable[[str, str, str], object] | None = None,
) -> None:
    """Applies and unapplies the migrations of the configured apps to reach ``target`` - every
    app's latest without it; a target before the applied migrations unapplies back to it,
    ``app.zero`` unapplies every migration of the app."""
    config = HareConfig.load(config).to_dict()
    config["apps"] = MigrationsModules.get_apps_with_existing_modules(config["apps"])

    await Hare.init(config=config, connect=False)

    configured_apps = config.get("apps", {})
    selected_apps = list(app_labels) if app_labels else list(configured_apps.keys())
    apps_by_connection = MigrationRequestParser.group_selected_apps_by_connection(
        configured_apps, selected_apps, DEFAULT_CONNECTION_NAME
    )

    targets = MigrationRequestParser.parse_targets(target, selected_apps)
    for connection_name, subset in apps_by_connection.items():
        connection = Connections.get(connection_name)
        # Every configured app is loaded: a dependency may name another connection's migration. The
        # executor applies only `subset`.
        executor = MigrationExecutor(connection, subset, full_apps_config=configured_apps)
        executor_targets = [t for t in targets if t.app_label in subset]
        if target is not None and not executor_targets:
            # The targets name no app on this connection - nothing to do here. An empty target list
            # would mean "every app to its latest".
            continue
        if reporter is not None:
            plan = await executor.plan(executor_targets if executor_targets else None)
            result = reporter(connection_name, plan, fake, dry_run)
            if inspect.isawaitable(result):
                await result
        await executor.migrate(
            executor_targets if executor_targets else None,
            fake=fake,
            dry_run=dry_run,
            progress=progress,
        )
