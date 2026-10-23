from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hare import Hare
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.migrations.constants import LATEST_MIGRATION
from hare.migrations.exceptions import UnknownMigrationError
from hare.migrations.execution.executor.migration_target import MigrationTarget
from hare.migrations.loading.migrations_modules import MigrationsModules


class MigrationRequestParser:
    """Resolves a migrate()/plan()/sqlmigrate() request's app_labels/target arguments into
    concrete MigrationTargets and per-connection app groupings."""

    @staticmethod
    async def init_selected_apps(
        config: dict[str, Any], app_labels: Sequence[str] | None
    ) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, dict[str, dict[str, Any]]]]:
        """Initializes hare without connecting for a request on the apps with migrations, and selects the
        apps it is about.

        Args:
            config: The configuration, as a dict - its apps narrowed to those with a migrations
                module.
            app_labels: The apps the request names - every configured one when empty.

        Returns:
            The configured apps, the selected ones, and the selected ones by connection.

        Raises:
            UnknownMigrationError: ``app_labels`` names an app that isn't configured.
        """
        config["apps"] = MigrationsModules.get_apps_with_existing_modules(config["apps"])
        await Hare.init(config=config, connect=False)
        configured_apps = config.get("apps", {})
        selected_apps = list(app_labels) if app_labels else list(configured_apps.keys())
        apps_by_connection = MigrationRequestParser.group_selected_apps_by_connection(
            configured_apps, selected_apps, DEFAULT_CONNECTION_NAME
        )
        return configured_apps, selected_apps, apps_by_connection

    @staticmethod
    def parse_targets(target: str | None, app_labels: Sequence[str]) -> list[MigrationTarget]:
        """Parses a migrate()/plan() `target` argument ("app_label" or "app_label.migration_name")
        into one MigrationTarget per selected app.

        Raises:
            UnknownMigrationError: If `target` names an app label not in `app_labels`.
        """
        if target is None:
            return [MigrationTarget(app_label=label, name=LATEST_MIGRATION) for label in app_labels]
        if "." in target:
            app_label, name = target.split(".", 1)
            if app_label not in app_labels:
                raise UnknownMigrationError(f"Unknown app label {app_label}")
            return [MigrationTarget(app_label=app_label, name=name)]
        if target not in app_labels:
            raise UnknownMigrationError(f"Unknown app label {target}")
        return [MigrationTarget(app_label=target, name=LATEST_MIGRATION)]

    @staticmethod
    def group_selected_apps_by_connection(
        configured_apps: dict[str, dict[str, Any]],
        selected_apps: Sequence[str],
        default_connection_name: str,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Groups ``selected_apps`` by their ``default_connection``. Each group holds only its own apps
        - an executor resolving a dependency on another connection's app gets every configured app
        through ``full_apps_config``.

        Raises:
            UnknownMigrationError: ``selected_apps`` names an app that isn't configured.
        """
        for label in selected_apps:
            if label not in configured_apps:
                raise UnknownMigrationError(f"Unknown app label {label}")

        apps_by_connection: dict[str, dict[str, dict[str, Any]]] = {}
        for label in selected_apps:
            app_config = configured_apps[label]
            connection_alias = app_config.get("default_connection", default_connection_name)
            apps_by_connection.setdefault(connection_alias, {})[label] = app_config
        return apps_by_connection
