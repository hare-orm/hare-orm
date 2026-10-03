from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hare.core.config import HareConfig
from hare.core.context import HareContext
from hare.exceptions import ConfigurationError
from hare.migrations.api.migration_changes import MigrationChanges
from hare.migrations.api.migration_maker import MigrationMaker
from hare.migrations.loading.migrations_modules import MigrationsModules


async def makemigrations(
    *,
    config: Mapping[str, Any] | HareConfig | str,
    app_labels: Sequence[str] | None = None,
    empty: bool = False,
    merge: bool = False,
    name: str | None = None,
) -> MigrationChanges:
    """The migrations taking each app's migration history to its models - not written yet: write
    each with ``writer.write()``. Creates an app's migrations package when it has none. Every
    configured app is loaded - a migration may depend on another app's.

    Args:
        config: The configuration, as for ``Hare.init()``.
        app_labels: The apps to make migrations for - every app without them.
        empty: Make one empty migration per app, to fill by hand.
        merge: Make one migration per app merging its forked history.
        name: The name the migrations get instead of a generated one.

    Returns:
        The changes.

    Raises:
        ConfigurationError: An unknown app, ``empty``/``merge`` without apps or together, a forked
            history (without ``merge``), or a history with nothing to merge.
    """
    if empty and merge:
        raise ConfigurationError("empty and merge can't be combined")
    if (empty or merge) and not app_labels:
        raise ConfigurationError("empty and merge need the apps to make migrations for")
    typed_config = HareConfig.load(config)
    config_dict = typed_config.to_dict()
    target_app_labels = list(app_labels) if app_labels else list(config_dict["apps"])
    for label in target_app_labels:
        if label not in config_dict["apps"]:
            raise ConfigurationError(f"Unknown app label {label}")
    config_dict["apps"] = MigrationsModules.get_apps_with_packages(config_dict["apps"])
    async with HareContext() as context:
        await context.init(config_dict, connect=False)
        maker = MigrationMaker(context.apps, config_dict["apps"])  # type: ignore[arg-type]
        return await maker.make(target_app_labels, empty=empty, merge=merge, name=name)
