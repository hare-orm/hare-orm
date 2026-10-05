from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hare import Hare
from hare.core.config import HareConfig
from hare.core.connections.connections import Connections
from hare.core.constants import DEFAULT_CONNECTION_NAME, DEFAULT_LARGE_TABLE_ROWS
from hare.migrations.api.migration_request_parser import MigrationRequestParser
from hare.migrations.execution.executor.migration_executor import MigrationExecutor
from hare.migrations.loading.migrations_modules import MigrationsModules
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
    config_dict["apps"] = MigrationsModules.get_apps_with_existing_modules(config_dict["apps"])

    await Hare.init(config=config_dict, connect=False)

    configured_apps = config_dict.get("apps", {})
    selected_apps = list(app_labels) if app_labels else list(configured_apps.keys())
    apps_by_connection = MigrationRequestParser.group_selected_apps_by_connection(
        configured_apps, selected_apps, DEFAULT_CONNECTION_NAME
    )
    risks: list[MigrationRisk] = []
    for connection_alias, subset in apps_by_connection.items():
        # Every configured app is loaded: a dependency may name another connection's migration.
        executor = MigrationExecutor(Connections.get(connection_alias), subset, full_apps_config=configured_apps)
        risks.extend(await executor.check_safety(checker))
    return risks
