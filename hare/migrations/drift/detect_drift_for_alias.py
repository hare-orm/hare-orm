from __future__ import annotations

from typing import Any

from hare.core.apps import Apps
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.exceptions import ConfigurationError
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.drift.detect_drift import detect_drift
from hare.migrations.drift.drift_result import DriftResult
from hare.migrations.state.model_state import ModelState


async def detect_drift_for_alias(
    apps: Apps,
    apps_config: dict[str, dict[str, Any]],
    connection_alias: str,
    *,
    schema: str | None = None,
    app_labels: list[str] | None = None,
) -> DriftResult:
    """Runs ``detect_drift()`` for every app on connection ``connection_alias``. An app is on it when
    its ``default_connection`` in ``apps_config`` is ``connection_alias``; an app registered at runtime
    when any of its models is.

    Args:
        apps: The initialized model registry.
        apps_config: Every configured app, by label.
        connection_alias: The connection alias.
        schema: The schema swept for untracked tables. Ignored on a database without schemas.
        app_labels: The app labels to check, in order - every app on ``connection_alias`` when omitted.

    Returns:
        The drift found.

    Raises:
        ConfigurationError: ``connection_alias`` isn't configured, an app label is unknown, or no app
            uses ``connection_alias``.
    """
    if connection_alias not in apps._connections.db_config:
        raise ConfigurationError(f'Unknown connection "{connection_alias}"')
    labels_on_alias = [
        label
        for label, app_config in apps_config.items()
        if app_config.get("default_connection", DEFAULT_CONNECTION_NAME) == connection_alias
    ]
    labels_on_alias += [
        label
        for label, models in apps.items()
        if label not in apps_config
        and any(model._meta.default_connection == connection_alias for model in models.values())
    ]
    if app_labels is None:
        target_labels = labels_on_alias
    else:
        unknown_app_labels = [label for label in app_labels if label not in apps_config and label not in apps]
        if unknown_app_labels:
            raise ConfigurationError(f"Unknown app label(s): {', '.join(unknown_app_labels)}")
        target_labels = [label for label in app_labels if label in labels_on_alias]
    if not target_labels:
        raise ConfigurationError(f'No app uses connection "{connection_alias}"')

    new_state = MigrationAutodetector(apps, apps_config).current_state()
    # A runtime-registered model can share an app label with models bound to another
    # connection - only the ones on `connection_alias` belong to this check. A swapped model has no table.
    for app_label, model_name in list(new_state.models):
        if app_label not in target_labels:
            continue
        if new_state.is_swapped_model(app_label, model_name):
            del new_state.models[(app_label, model_name)]
            continue
        model_connection = apps[app_label][model_name]._meta.default_connection
        if model_connection is not None and model_connection != connection_alias:
            del new_state.models[(app_label, model_name)]
    unmanaged_model_states = [
        ModelState.make_from_model(app_label, model)
        for app_label in target_labels
        for model in apps.apps.get(app_label, {}).values()
        if model._meta.managed is False and model._meta.default_connection in (connection_alias, None)
    ]
    connection = apps._connections.get(connection_alias)
    return await detect_drift(connection, new_state, target_labels, schema, unmanaged_model_states)
