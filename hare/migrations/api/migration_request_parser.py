from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hare.migrations.constants import LATEST_MIGRATION
from hare.migrations.exceptions import UnknownMigrationError
from hare.migrations.execution.executor.migration_target import MigrationTarget


class MigrationRequestParser:
    """Resolves a migrate()/plan()/sqlmigrate() request's app_labels/target arguments into
    concrete MigrationTargets and per-connection app groupings."""

    @staticmethod
    def parse_targets(target: str | None, app_labels: Sequence[str]) -> list[MigrationTarget]:
        """Parses a migrate()/plan() `target` argument ("app_label" or "app_label.migration_name")
        into one MigrationTarget per selected app.

        Raises:
            UnknownMigrationError: If `target` names an app label not in `app_labels`.
        """
        if not target:
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
