from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hare.core.config import HareConfig
from hare.core.constants import DEFAULT_LARGE_TABLE_ROWS
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from hare.migrations.loading.migrations_modules import MigrationsModules
from hare.migrations.making.migration_changes import MigrationChanges
from hare.migrations.making.migration_maker import MigrationMaker


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
    safety_config = typed_config.migrations.safety if typed_config.migrations is not None else None
    async with HareContext() as context:
        await context.init(config_dict, connect=False)
        maker = MigrationMaker(
            context.apps,  # type: ignore[arg-type]
            config_dict["apps"],
            large_table_rows=safety_config.large_table_rows if safety_config is not None else DEFAULT_LARGE_TABLE_ROWS,
        )
        return await maker.make(target_app_labels, empty=empty, merge=merge, name=name)
