from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hare.core.config import HareConfig
from hare.core.context import HareContext
from hare.exceptions import ConfigurationError
from hare.migrations.api.migration_maker import MigrationMaker
from hare.migrations.api.squashed_migration import SquashedMigration
from hare.migrations.loading.migrations_modules import MigrationsModules


async def squashmigrations(
    *, config: Mapping[str, Any] | HareConfig | str, app_label: str, name: str | None = None
) -> SquashedMigration:
    """One migration creating an app's models from nothing, to replace all its migrations - not
    written yet. It records the migrations it replaces, but the loader doesn't read that: it is
    safe only for a database where the app's migrations were never applied. A ``RunPython``/
    ``RunSQL`` of the old migrations isn't carried over - they are named in the result.

    Args:
        config: The configuration, as for ``Hare.init()``.
        app_label: The app.
        name: The squashed migration's name, ``squashed`` without one.

    Returns:
        The squashed migration - without a writer when the app has one migration.

    Raises:
        ConfigurationError: An unknown app, or one without migrations.
    """
    typed_config = HareConfig.load(config)
    config_dict = typed_config.to_dict()
    if app_label not in config_dict["apps"]:
        raise ConfigurationError(f"Unknown app label: {app_label}")
    config_dict["apps"] = MigrationsModules.get_apps_with_packages(config_dict["apps"])
    async with HareContext() as context:
        await context.init(config_dict, connect=False)
        maker = MigrationMaker(context.apps, config_dict["apps"])  # type: ignore[arg-type]
        return await maker.squash(app_label, name)
