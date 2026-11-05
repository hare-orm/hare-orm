from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hare.core.config import HareConfig
from hare.core.constants import DEFAULT_LARGE_TABLE_ROWS
from hare.migrations.api.migration_session import MigrationSession
from hare.migrations.execution.connections_plan import ConnectionsPlan
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
    """
    hare_config = HareConfig.load(config)
    safety_config = hare_config.migrations.safety if hare_config.migrations is not None else None
    checker = MigrationSafetyChecker(
        large_table_rows=safety_config.large_table_rows if safety_config is not None else DEFAULT_LARGE_TABLE_ROWS
    )
    async with MigrationSession(hare_config, app_labels) as session:
        return await ConnectionsPlan(session.create_executors()).check_safety(checker)
