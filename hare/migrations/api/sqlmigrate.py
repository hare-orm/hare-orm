from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hare import Hare
from hare.core.config import HareConfig
from hare.core.connections.connections import Connections
from hare.migrations.exceptions import UnknownMigrationError
from hare.migrations.execution.executor.migration_executor import MigrationExecutor
from hare.migrations.loading.migrations_modules import MigrationsModules
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
    config = HareConfig.load(config).to_dict()
    config["apps"] = MigrationsModules.get_apps_with_existing_modules(config["apps"])

    await Hare.init(config=config, connect=False)

    configured_apps = config.get("apps", {})
    if app_label not in configured_apps:
        raise UnknownMigrationError(f"Unknown app label {app_label!r}")

    app_config = configured_apps[app_label]
    connection_alias = app_config.get("default_connection", "default")
    connection = Connections.get(connection_alias)

    # Every configured app's migrations are loaded - a dependency may name another app's.
    executor = MigrationExecutor(connection, configured_apps)
    # Replace the recorder with a noop so build_graph() does not query the DB.
    executor.loader.recorder = NoopRecorder()

    return await executor.collect_sql(app_label, migration_name, backward=backward)
