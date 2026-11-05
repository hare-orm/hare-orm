from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.migrations.state.model_state import ModelState

from hare.core.apps import Apps
from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.core.apps.relation_linking import RelationLinking
from hare.core.connections.connection_handler import ConnectionHandler
from hare.exceptions import ConfigurationError
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.state.migration_environment import MigrationEnvironment
from hare.models import Model


class StateApps(Apps):
    """The registry of the model classes a migration state renders."""

    REJECTS_SWAPPED_RELATION_TARGETS = False

    def __init__(
        self,
        default_connections: dict[str, str] | None = None,
        connections: ConnectionHandler | None = None,
        *,
        environment: MigrationEnvironment | None = None,
    ) -> None:
        self.environment = environment if environment is not None else MigrationEnvironment.capture(connections)
        super().__init__({}, self.environment.connections, swappable=self.environment.swappable_settings)
        self._default_connections = default_connections or {}

    def register_model(self, app_label: str, model: type[Model]) -> None:
        """Registers a model class the state renders under its app, with the app's default connection.

        Args:
            app_label: The app.
            model: The model.

        Raises:
            ConfigurationError: The model is registered under another app.
        """
        if model._meta.app and model._meta.app != app_label:
            raise ConfigurationError(f"Given model is already registered with label {model._meta.app}")

        graph = ModelBindingGraph.current.get()
        if graph is not None and graph.apps is self:
            graph.include((model,))
        self.apps.setdefault(app_label, {})[model.__name__] = model
        model._meta.app = app_label
        if app_label in self._default_connections:
            model._meta.default_connection = self._default_connections[app_label]

    def get_identifier_key(self, model: type[Model], name: str) -> str:
        if self._connections._db_config is None or model._meta.default_connection is None:
            # A state rendered without connections - its models were checked as they were declared.
            return name
        return super().get_identifier_key(model, name)

    def get_swappable_label(self, setting: str) -> str:
        """Read captured application settings, falling back to historical declarations."""
        return self.environment.get_swappable_label(setting) or super().get_swappable_label(setting)

    def get_model_reference(self, reference: Any) -> Any:
        """Bind a reference against this registry, independent of the current context."""
        return (
            self.get_swappable_label(reference.setting)
            if isinstance(reference, SwappableModelReference)
            else reference
        )

    def _reference_is_missing(self, reference: Any) -> bool:
        """Whether a relation's ``"app.Model"`` reference names a model this state can't load
        yet - a model class, or a reference of another form, never is.

        Args:
            reference: The reference.

        Returns:
            True for an unloadable ``"app.Model"`` string.
        """
        if not isinstance(reference, str):
            return False
        parts = reference.split(".")
        return len(parts) == 2 and not self._reference_is_loadable(*parts)

    def _reference_is_loadable(self, reference_app: str, reference_model: str) -> bool:
        """Whether a relation's target can be resolved now: tracked in this state, or a ``Meta.managed
        = False`` model, which never gets a ``CreateModel`` - a state-only clone of it is
        registered.
        """
        if reference_app in self.apps and reference_model in self.apps[reference_app]:
            return True
        model_state = self.environment.unmanaged_models.get((reference_app, reference_model))
        if model_state is None:
            return False
        self.register_model(reference_app, model_state.render())
        return True

    def _check_table_name_collisions(self) -> None:
        """No-op - a model moved to another app is recorded under both apps for the span of
        migrations between its state-only CreateModel and DeleteModel, one table for both."""

    def init_relations(self) -> None:
        """Sets up relations, skipping those whose target isn't registered yet (``CreateModel``
        operations come in any order) - such a model stays not inited until the target exists.
        """
        if all(model._meta._inited for app in self.apps.values() for model in app.values()):
            return
        super().init_relations()

    def link_relations(self) -> None:
        uninited_models = [model for app in self.apps.values() for model in app.values() if not model._meta._inited]
        if not uninited_models:
            return

        models_with_missing_references: set[type[Model]] = set()
        # The models only a many-to-many field's target or through model of is missing - their
        # foreign keys are linked now, as their table is created with those columns.
        models_missing_only_many_to_many: set[type[Model]] = set()

        for model in uninited_models:
            for field_name in (*model._meta.foreign_key_fields, *model._meta.one_to_one_fields):
                foreign_key_object = model._meta.fields_map[field_name]
                reference = self.get_model_reference(foreign_key_object.model_name)  # type: ignore[attr-defined]
                if self._reference_is_missing(reference):
                    models_with_missing_references.add(model)
                    break  # No need to check remaining fields

            if model in models_with_missing_references:
                continue

            for field_name in model._meta.many_to_many_fields:
                many_to_many_object = model._meta.fields_map[field_name]
                references = [self.get_model_reference(many_to_many_object.model_name)]  # type: ignore[attr-defined]
                through_reference = self.get_model_reference(many_to_many_object.through_model)  # type: ignore[attr-defined]
                if through_reference is not None:
                    # A through model referenced as "app.Model" must be registered too.
                    references.append(through_reference)
                if any(self._reference_is_missing(reference) for reference in references):
                    models_with_missing_references.add(model)
                    models_missing_only_many_to_many.add(model)
                    break

        for model in models_with_missing_references:
            model._meta._inited = True

        super().link_relations()

        for model in models_with_missing_references:
            model._meta._inited = False
        for model in models_missing_only_many_to_many:
            RelationLinking.ensure_foreign_key_or_one_to_one_inited(self, model)

    def build_initial_querysets(self) -> None:
        self.build_querysets(model for app in self.apps.values() for model in app.values())

    def build_querysets(self, models: Iterable[type[Model]]) -> None:
        """Finalises the inited models of ``models`` and builds their base queries.

        Args:
            models: The models.
        """
        # Skip building querysets when no DB config is available (state-only mode)
        # This allows pure state operations to work without database connections
        if self._connections._db_config is None:
            return

        models = tuple(models)
        with ModelBindingGraph(self, models):
            for model in models:
                if model._meta.default_connection is None:
                    continue
                if not model._meta._inited:
                    continue
                model._meta.finalise_model()
                connection = self._connections.get(model._meta.default_connection)
                self.bind_model_base_queries(model, connection)

    def unregister_model(self, app_label: str, model_name: str) -> None:
        try:
            model = self.apps[app_label].pop(model_name)
            model._meta.app = None
        except KeyError:
            return

    @staticmethod
    def split_reference(reference: str | type[Model]) -> tuple[str, str]:
        """A model reference as its app label and model name.

        Args:
            reference: ``"app.Model"`` or a model class; bind swappable settings before calling.

        Returns:
            The app label and the model name.

        Raises:
            ConfigurationError: The reference isn't of two parts, or the model is registered in no app.
        """
        if not isinstance(reference, str):
            model_class = reference
            app_label = model_class._meta.app
            if app_label is None:
                raise ConfigurationError(f"Model {model_class} is not registered in any app")
            return app_label, model_class.__name__
        if len(items := reference.split(".")) != 2:
            raise ConfigurationError(f"{reference!r} is not a valid model reference. Should be <app>.<model>.")
        return items[0], items[1]

    def clone(
        self,
        model_states: dict[tuple[str, str], ModelState] | None = None,
    ) -> StateApps:
        """A copy of the registry - its models rendered again from their states.

        Args:
            model_states: The states of the models, by app label and model name.

        Returns:
            The copy.
        """
        # Imported here: the model state module imports this one.
        from hare.migrations.state.model_state import ModelState

        state_apps = self.__class__(
            default_connections=dict(self._default_connections),
            environment=self.environment,
        )
        with ModelBindingGraph(state_apps):
            if model_states is not None:
                for (app_label, _model_name), model_state in model_states.items():
                    # Rendered from copies: relation setup fills derived attributes into the fields,
                    # which must not leak into the state.
                    model = model_state.render()
                    state_apps.register_model(app_label, model)
            else:
                for app_label, app in self.apps.items():
                    for model in app.values():
                        model_clone = ModelState.make_from_model(app_label, model).render()
                        state_apps.register_model(app_label, model_clone)

            state_apps.init_relations()
            state_apps.build_initial_querysets()
        return state_apps
