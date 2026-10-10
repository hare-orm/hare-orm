from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from hare.migrations.loading.migrations_modules import MigrationsModules
from hare.migrations.making.migration_squasher import MigrationSquasher
from hare.migrations.making.squashed_migration import SquashedMigration


async def squashmigrations(
    *,
    config: Mapping[str, Any] | HareConfig | str,
    app_label: str,
    end_name: str,
    start_name: str | None = None,
    squashed_name: str | None = None,
) -> SquashedMigration:
    """One migration replacing a run of an app's migrations - not written yet. Their operations
    are kept in order and shortened (a field added and removed cancels out); a ``RunPython``/
    ``RunSQL`` is kept unless it's ``elidable``. The migration names the ones it replaces in
    ``replaces``: on a database where none or all of them are applied it runs in their place, and
    where only some are, they run on their own until all are. Delete the replaced files once every
    database has applied them all.

    Args:
        config: The configuration, as for ``Hare.init()``.
        app_label: The app.
        end_name: The last migration squashed - its name or a prefix of it.
        start_name: The first one - the app's first migration when None.
        squashed_name: The new migration's name after its number - ``squashed_<end>`` when None.

    Returns:
        The squashed migration - without a writer when the range holds one migration.

    Raises:
        ConfigurationError: An unknown app or migration, a start after the end, or a taken name.
    """
    typed_config = HareConfig.load(config)
    config_dict = typed_config.to_dict()
    if app_label not in config_dict["apps"]:
        raise ConfigurationError(f"Unknown app label: {app_label}")
    config_dict["apps"] = MigrationsModules.get_apps_with_packages(config_dict["apps"])
    async with HareContext() as context:
        await context.init(config_dict, connect=False)
        squasher = MigrationSquasher(config_dict["apps"])
        return await squasher.squash(app_label, end_name, start_name=start_name, squashed_name=squashed_name)
