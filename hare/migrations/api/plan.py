from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hare.core.config import HareConfig
from hare.core.connections.connections import Connections
from hare.migrations.api.migration_request_parser import MigrationRequestParser
from hare.migrations.execution.connections_plan import ConnectionsPlan
from hare.migrations.execution.executor.migration_executor import MigrationExecutor


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
    configured_apps, selected_apps, apps_by_connection = await MigrationRequestParser.init_selected_apps(
        config, app_labels
    )

    targets = MigrationRequestParser.parse_targets(target, selected_apps)
    # Every configured app is loaded by each executor: a dependency may name another connection's
    # migration. One plan over them all orders the migrations of every connection.
    executors = {
        connection_alias: MigrationExecutor(
            Connections.get(connection_alias), subset, full_apps_config=configured_apps
        )
        for connection_alias, subset in apps_by_connection.items()
    }
    output = await ConnectionsPlan(executors).get_plan_lines(targets)
    for line in output:
        print(line)
    return output
