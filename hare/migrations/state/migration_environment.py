from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hare.core.apps import Apps
from hare.core.connections.connection_handler import ConnectionHandler
from hare.core.hare_context import HareContext

if TYPE_CHECKING:
    from hare.migrations.state.model_state import ModelState


@dataclass(frozen=True)
class MigrationEnvironment:
    """Application inputs retained by a migration state and all its copies.

    Connections are shared resources, never copied or closed by a state. Swappable
    settings and unmanaged model declarations are captured once, before deferred
    rendering. Switching the current application afterwards cannot change them.
    An explicitly constructed empty environment supports offline state work.
    """

    connections: ConnectionHandler = field(default_factory=ConnectionHandler)
    swappable_settings: dict[str, str | dict[str, str]] = field(default_factory=dict)
    unmanaged_models: dict[tuple[str, str], ModelState] = field(default_factory=dict)

    @classmethod
    def capture(cls, connections: ConnectionHandler | None = None) -> MigrationEnvironment:
        """Capture the current application's configuration without database IO."""
        context = HareContext.get_current()
        apps = context.apps if context is not None else None
        if connections is None:
            connections = context.connections if context is not None else ConnectionHandler()
        return cls.from_apps(apps, connections=connections) if apps is not None else cls(connections)

    @classmethod
    def from_apps(cls, apps: Apps, *, connections: ConnectionHandler | None = None) -> MigrationEnvironment:
        """Capture an explicit registry without consulting the current application.

        The registry's connection handler is retained unless a caller supplies another
        one. Model declarations and settings are copied; connection resources are shared.
        """
        from hare.migrations.state.model_state import ModelState

        settings = deepcopy(apps.swappable_settings)
        unmanaged = {}
        for app_label, models in apps.apps.items():
            for name, model in models.items():
                setting = model._meta.swappable
                if setting is not None and setting not in settings:
                    settings[setting] = apps.get_swappable_label(setting)
                if model._meta.managed is False:
                    unmanaged[(app_label, name)] = ModelState.make_from_model(
                        app_label, model, with_schema_objects=False
                    )
        return cls(connections if connections is not None else apps._connections, settings, unmanaged)

    def get_swappable_label(self, setting: str) -> str | None:
        """Read a captured setting; absent settings may use historical declarations."""
        if setting not in self.swappable_settings:
            return None
        return Apps({}, self.connections, swappable=self.swappable_settings).get_swappable_label(setting)
