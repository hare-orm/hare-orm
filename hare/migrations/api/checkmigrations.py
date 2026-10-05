from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hare.core.config import HareConfig
from hare.core.connections.connections import Connections
from hare.core.constants import DEFAULT_LARGE_TABLE_ROWS
from hare.migrations.api.migration_request_parser import MigrationRequestParser
from hare.migrations.execution.connections_plan import ConnectionsPlan
from hare.migrations.execution.executor.migration_executor import MigrationExecutor
from hare.migrations.safety.migration_risk import MigrationRisk
from hare.migrations.safety.migration_safety_checker import MigrationSafetyChecker


async def checkmigrations(
    *,
    config: Mapping[str, Any] | HareConfig | str,
    app_labels: Sequence[str] | None = None,
) -> list[MigrationRisk]:
    """Checks the migrations ``migrate`` would apply against the live database: the operations that
    lock or rewrite a table in use, or break the code still running during a deployment. A table
    counts as large from the config's ``migrations.safety.large_table_rows`` rows.

    Args:
        config: The configuration, in any form ``HareConfig.load()`` takes.
        app_labels: The apps whose migrations are checked - every app without them.

    Returns:
        The risks, in the order the migrations would be applied - those a migration exempts in
        ``safety_exemptions`` included, marked ``exempted``.

    Raises:
        UnknownMigrationError: ``app_labels`` names an app that isn't configured.
        ConfigurationError: A migration's ``safety_exemptions`` holds something other than a
            ``MigrationRiskCode``.
    """
    hare_config = HareConfig.load(config)
    safety_config = hare_config.migrations.safety if hare_config.migrations is not None else None
    checker = MigrationSafetyChecker(
        large_table_rows=safety_config.large_table_rows if safety_config is not None else DEFAULT_LARGE_TABLE_ROWS
    )
    config_dict = hare_config.to_dict()
    configured_apps, selected_apps, apps_by_connection = await MigrationRequestParser.init_selected_apps(
        config_dict, app_labels
    )
    # Every configured app is loaded by each executor: a dependency may name another connection's
    # migration.
    executors = {
        connection_alias: MigrationExecutor(
            Connections.get(connection_alias), subset, full_apps_config=configured_apps
        )
        for connection_alias, subset in apps_by_connection.items()
    }
    return await ConnectionsPlan(executors).check_safety(checker)
