from __future__ import annotations

from collections.abc import Collection
from typing import Any

from hare.core.apps import Apps
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.exceptions import ConfigurationError
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.drift.drift_result import DriftResult
from hare.migrations.drift.shadow.shadow_drift import ShadowDrift


async def detect_drift_for_alias(
    apps: Apps,
    apps_config: dict[str, dict[str, Any]],
    connection_alias: str,
    *,
    schema: str | None = None,
    app_labels: list[str] | None = None,
    journal_tables: Collection[str] = (),
) -> DriftResult:
    """Runs ``detect_drift()`` for every app on connection ``connection_alias``. An app is on it when
    its ``default_connection`` in ``apps_config`` is ``connection_alias``; an app registered at runtime
    when any of its models is.

    Args:
        apps: The initialized model registry.
        apps_config: Every configured app, by label.
        connection_alias: The connection alias.
        schema: The schema swept for untracked tables - a schema, an attached database or a database of
            the server - the namespace of tables the database has; the connection's default one when None.
        app_labels: The app labels to check, in order - every app on ``connection_alias`` when omitted.
        journal_tables: The tables of migration journals besides ``hare_migrations`` - a
            ``MigrationRecorder(table_name=...)``'s - known too.

    Returns:
        The drift found.

    Raises:
        ConfigurationError: ``connection_alias`` isn't configured, an app label is unknown, or no app
            uses ``connection_alias``.
    """
    if connection_alias not in apps._connections.db_config:
        raise ConfigurationError(f"Unknown connection {connection_alias!r}")
    labels_on_alias = [
        label
        for label, app_config in apps_config.items()
        if app_config.get("default_connection", DEFAULT_CONNECTION_NAME) == connection_alias
    ]
    labels_on_alias += [
        label
        for label, models in apps.items()
        if label not in apps_config
        and any(apps.get_binding(model).default_connection == connection_alias for model in models.values())
    ]
    if app_labels is None:
        target_labels = labels_on_alias
    else:
        unknown_app_labels = [label for label in app_labels if label not in apps_config and label not in apps]
        if unknown_app_labels:
            raise ConfigurationError(f"Unknown app label(s): {', '.join(unknown_app_labels)}")
        target_labels = [label for label in app_labels if label in labels_on_alias]
    if not target_labels:
        raise ConfigurationError(f"No app uses connection {connection_alias!r}")

    bindings_on_connection = {
        (app_label, model_name): model_binding
        for app_label, models in apps.items()
        for model_name, model in models.items()
        if (model_binding := apps.get_binding(model)).default_connection in (None, connection_alias)
    }
    new_state = MigrationAutodetector(apps, apps_config).current_state()
    for key in list(new_state.models):
        if key[0] in target_labels and (new_state.is_swapped_model(*key) or key not in bindings_on_connection):
            del new_state.models[key]
    unmanaged_model_states = [
        new_state.environment.get_model_state(app_label, binding)
        for app_label in target_labels
        for model_name in apps.apps.get(app_label, {})
        if (binding := bindings_on_connection.get((app_label, model_name))) is not None and binding.managed is False
    ]
    connection = apps._connections.get(connection_alias)
    return await ShadowDrift(
        connection,
        new_state,
        target_labels,
        schema,
        unmanaged_model_states,
        journal_tables,
        connection_model_keys=bindings_on_connection,
    ).detect()
