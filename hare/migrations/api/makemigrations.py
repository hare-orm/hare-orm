from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.core.apps.model_connection_checks import ModelConnectionChecks
from hare.core.config import HareConfig
from hare.core.constants import DEFAULT_LARGE_TABLE_ROWS
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from hare.migrations.autodetection.rename_questions import RenameQuestions
from hare.migrations.loading.migrations_modules import MigrationsModules
from hare.migrations.making.migration_changes import MigrationChanges
from hare.migrations.making.migration_maker import MigrationMaker

if TYPE_CHECKING:
    from hare.core.apps import Apps


async def makemigrations(
    *,
    config: Mapping[str, Any] | HareConfig | str,
    app_labels: Sequence[str] | None = None,
    empty: bool = False,
    merge: bool = False,
    name: str | None = None,
    rename_questions: RenameQuestions | None = None,
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
        rename_questions: The answers whether a removed field, model or app's model is renamed or
            moved (``RenameQuestions``) - none renamed without them.

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
    # An app named twice is one app - one migration of it.
    target_app_labels = list(dict.fromkeys(app_labels)) if app_labels else list(config_dict["apps"])
    for label in target_app_labels:
        if label not in config_dict["apps"]:
            raise ConfigurationError(f"Unknown app label {label}")
    config_dict["apps"] = MigrationsModules.get_apps_with_packages(config_dict["apps"])
    safety_config = typed_config.migrations.safety if typed_config.migrations is not None else None
    async with HareContext() as context:
        await context.init(config_dict, connect=False)
        apps = cast("Apps", context.apps)
        # A view declared as a queryset is written as that queryset's SQL.
        apps.bind_base_queries()
        # A constraint no migrate could apply isn't written into a migration.
        for app_models in apps.apps.values():
            for model in app_models.values():
                if model._meta.connection.features.supports_unique_constraints:
                    ModelConnectionChecks.raise_if_unique_constraint_options_unsupported(model)
        maker = MigrationMaker(
            apps,
            config_dict["apps"],
            large_table_rows=safety_config.large_table_rows if safety_config is not None else DEFAULT_LARGE_TABLE_ROWS,
            rename_questions=rename_questions,
        )
        return await maker.make(target_app_labels, empty=empty, merge=merge, name=name)
