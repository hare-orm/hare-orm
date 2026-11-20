from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hare.core.apps.swappable_models import SwappableModels
from hare.core.connections.connection_handler import ConnectionHandler
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.core.apps import Apps
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.migrations.state.model_state import ModelState
    from hare.models.model import Model
    from hare.models.model_binding import ModelBinding


@dataclass(frozen=True)
class MigrationEnvironment:
    """Application inputs retained by a migration state and all its copies.

    Connections are shared resources, never copied or closed by a state. Swappable
    settings and unmanaged model declarations are captured once, before deferred
    rendering. Switching the current application afterwards cannot change them.
    An explicitly constructed empty environment supports offline state work.
    """

    connections: ConnectionHandler = field(default_factory=ConnectionHandler)
    context: HareContext | None = None
    source_bindings: dict[type[Model], ModelBinding] = field(default_factory=dict)
    swappable_settings: dict[str, str | dict[str, str]] = field(default_factory=dict)
    unmanaged_models: dict[tuple[str, str], ModelState] = field(default_factory=dict)

    @classmethod
    def capture(cls, connections: ConnectionHandler | None = None) -> MigrationEnvironment:
        """Capture the current application's configuration without database IO."""
        context = HareContext.get_current()
        if connections is not None and context is not None and connections is not context.connections:
            raise ConfigurationError(
                "Capturing migration settings for another connection owner requires "
                "explicit Apps or MigrationEnvironment"
            )
        apps = context.apps if context is not None else None
        if connections is None:
            connections = context.connections if context is not None else ConnectionHandler()
        return (
            cls.from_apps(apps, connections=connections)
            if apps is not None
            else cls(connections=connections, context=context)
        )

    @classmethod
    def from_apps(cls, apps: Apps, *, connections: ConnectionHandler | None = None) -> MigrationEnvironment:
        """Capture an explicit registry without consulting the current application.

        The registry's connection handler is retained unless a caller supplies another
        one. Model declarations and settings are copied; connection resources are shared.
        """
        from hare.migrations.state.model_state import ModelState
        from hare.migrations.state.state_apps import StateApps

        inherited = apps.environment if isinstance(apps, StateApps) else None
        settings = deepcopy(apps.swappable_settings)
        unmanaged = {} if inherited is None else dict(inherited.unmanaged_models)
        source_bindings = {} if inherited is None else dict(inherited.source_bindings)
        source_bindings.update((model, apps.get_binding(model)) for model in apps.get_models_iterable())
        for app_label, models in apps.apps.items():
            for name, model in models.items():
                binding = apps.get_binding(model)
                setting = binding.swappable
                if setting is not None and setting not in settings:
                    settings[setting] = apps.get_swappable_label(setting)
                if binding.managed is False:
                    unmanaged[(app_label, name)] = ModelState.make_from_binding(
                        app_label, binding, with_schema_objects=False
                    )
        return cls(
            connections=connections if connections is not None else apps._connections,
            context=apps.context,
            source_bindings=source_bindings,
            swappable_settings=settings,
            unmanaged_models=unmanaged,
        )

    def get_schema_client(self, app_label: str, default_connections: dict[str, str]) -> DatabaseClient | None:
        """Select configured compilation metadata from this captured environment, never ambient Apps."""
        alias = default_connections.get(app_label)
        if alias is None:
            aliases = {
                binding.default_connection for binding in self.source_bindings.values() if binding.app == app_label
            }
            aliases.discard(None)
            if len(aliases) == 1:
                alias = next(iter(aliases))
        return self.connections.get_main(alias) if alias in (self.connections._db_config or {}) else None

    def get_swappable_label(self, setting: str) -> str | None:
        """Read a captured setting; absent settings may use historical declarations."""
        if setting not in self.swappable_settings:
            return None
        return SwappableModels.get_single_model_label(setting, self.swappable_settings[setting])

    def get_bindings(
        self,
        models: list[type[Model]],
        *,
        app_label: str | None = None,
        default_connections: dict[str, str] | None = None,
    ) -> tuple[ModelBinding, ...]:
        """Capture registered inputs, or bind offline declarations with the same graph builder."""
        if all(model in self.source_bindings for model in models):
            return tuple(self.source_bindings[model] for model in models)
        from hare.core.apps.model_binding_preparation import ModelBindingPreparation
        from hare.migrations.state.state_apps import StateApps

        connections = dict(default_connections or {})
        for model in models:
            source = self.source_bindings.get(model)
            if source is not None and source.app is not None and source.default_connection is not None:
                connections.setdefault(source.app, source.default_connection)
        apps = StateApps(default_connections=connections, environment=self)
        with ModelBindingPreparation(apps, models):
            for model in models:
                source = self.source_bindings.get(model)
                label = source.app if source is not None else model._declaration.app or app_label
                if label is None:
                    raise ConfigurationError(f"Model {model.__name__} names no app - pass app_label")
                apps.register_model(label, model)
                if source is not None:
                    binding = apps.get_binding(model)
                    binding.managed = source.managed
                    binding.registered_live = source.registered_live
                    binding.db_table = source.db_table
                    # Snapshot the selected physical name rather than regenerating it from the class.
                    binding.generated_db_table = None
                    binding.schema = source.schema
            apps.init_relations()
            if self.connections._db_config is not None:
                apps.build_initial_querysets()
        return tuple(apps.get_binding(model) for model in models)

    def get_model_state(self, app_label: str, binding: ModelBinding) -> ModelState:
        """Encode schema queries under this environment without storing resources in the graph."""
        from hare.migrations.state.model_state import ModelState
        from hare.query.execution.query_execution import QueryExecution

        alias = binding.default_connection
        client = self.connections.get_main(alias) if alias in (self.connections._db_config or {}) else None
        with QueryExecution(self.context):
            return ModelState.make_from_binding(app_label, binding, client=client)
