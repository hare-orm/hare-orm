from collections.abc import Mapping, Sequence
from typing import Any

from hare import Hare
from hare.core.config import HareConfig
from hare.core.connections import Connections
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.migrations.api.common import MigrationRequestParser
from hare.migrations.execution.executor.migration_executor import MigrationExecutor
from hare.migrations.execution.executor.plan_step import PlanStep
from hare.migrations.loading.migrations_modules import MigrationsModules


async def plan(
    *,
    config: Mapping[str, Any] | HareConfig | str,
    app_labels: Sequence[str] | None = None,
    target: str | None = None,
) -> list[str]:
    """
    Print an ordered migration plan and return the formatted lines.
    """
    config = HareConfig.load(config).to_dict()
    config["apps"] = MigrationsModules.get_apps_with_existing_modules(config["apps"])

    await Hare.init(config=config, connect=False)

    configured_apps = config.get("apps", {})
    selected_apps = list(app_labels) if app_labels else list(configured_apps.keys())
    apps_by_connection = MigrationRequestParser.group_selected_apps_by_connection(
        configured_apps, selected_apps, DEFAULT_CONNECTION_NAME
    )

    targets = MigrationRequestParser.parse_targets(target, selected_apps)
    output: list[str] = []
    for connection_name, subset in apps_by_connection.items():
        connection = Connections.get(connection_name)
        # full_apps_config=configured_apps - see migrate()'s identical comment for why: a
        # cross-connection `dependencies` entry needs every configured app loaded to resolve,
        # even though this executor only ever reports migrations from `subset` itself.
        executor = MigrationExecutor(connection, subset, full_apps_config=configured_apps)
        executor_targets = [t for t in targets if t.app_label in subset]
        if target is not None and not executor_targets:
            # The explicit target names an app on another connection - an empty target list
            # would otherwise mean "every app on this connection to its latest".
            continue
        steps = await executor.plan(executor_targets if executor_targets else None)
        output.extend(PlanStep.format_steps(steps, connection_name))

    for line in output:
        print(line)
    return output
