from __future__ import annotations

from collections.abc import Mapping
from functools import cached_property
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.core.apps.declarations import ModelReference
    from hare.dialects.base.literals.sql_literals import SqlLiterals
    from hare.migrations.state.model_state import ModelState
    from hare.models.model_binding import ModelBinding

from hare.core.apps import Apps
from hare.core.apps.model_binding_preparation import ModelBindingPreparation
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
        super().__init__(
            {},
            self.environment.connections,
            swappable=self.environment.swappable_settings,
            context=self.environment.context,
        )
        self._default_connections = default_connections or {}

    def register_model(self, app_label: str, model: type[Model]) -> None:
        """Registers a model class the state renders under its app, with the app's default connection.

        Args:
            app_label: The app.
            model: The model.

        Raises:
            ConfigurationError: The model is registered under another app.
        """
        if self._preparation is None:
            with ModelBindingPreparation(self, (model,)):
                self.register_model(app_label, model)
                self.init_relations()
                self.build_initial_querysets()
            return
        if model._declaration.app and model._declaration.app != app_label:
            raise ConfigurationError(f"Given model is already registered with label {model._declaration.app}")

        self._preparation.include((model,))
        self.apps.setdefault(app_label, {})[model.__name__] = model
        binding = self.get_binding(model)
        binding.app = app_label
        if app_label in self._default_connections:
            binding.default_connection = self._default_connections[app_label]

    @cached_property
    def unbound_identifier_rules(self) -> SqlLiterals:
        """Generic, case-sensitive declaration rules for a state without connections."""
        from hare.dialects.base.dialect import Dialect

        return Dialect().literals

    def get_identifier_rules(self, model: type[Model]) -> SqlLiterals:
        if self._connections._db_config is None or self.get_binding(model).default_connection is None:
            return self.unbound_identifier_rules
        return super().get_identifier_rules(model)

    def get_swappable_label(self, setting: str) -> str:
        """Read captured application settings, falling back to historical declarations."""
        return self.environment.get_swappable_label(setting) or super().get_swappable_label(setting)

    def get_model(self, reference: Any) -> type[Model]:
        """Read historical identity before falling back to the common registry lookup."""
        return super().get_model(self.get_model_reference(reference))

    def get_binding(self, reference: Any) -> ModelBinding:
        """Map captured live references to this historical graph before reading metadata."""
        return super().get_binding(self.get_model_reference(reference))

    def get_model_reference(self, reference: ModelReference) -> ModelReference:
        """Bind a reference against this registry, independent of the current context."""
        if isinstance(reference, type) and reference in self.environment.source_bindings:
            binding = self.environment.source_bindings[reference]
            return f"{binding.app}.{reference.__name__}"
        if isinstance(reference, type) and reference._declaration.app is not None:
            # A migration's Python class names historical identity, never a live graph.
            return f"{reference._declaration.app}.{reference.__name__}"
        return (
            self.get_swappable_label(reference.setting)
            if isinstance(reference, SwappableModelReference)
            else reference
        )

    def _reference_is_missing(self, reference: Any) -> bool:
        """Whether a relation target is absent from this version of the historical registry.

        Labels can load a captured unmanaged declaration. Class references are first translated
        through captured placement or an explicit declaration app; another graph supplies no metadata.

        Args:
            reference: The reference.

        Returns:
            True for an unloadable ``"app.Model"`` string.
        """
        if isinstance(reference, type):
            return all(reference is not model for model in self.get_models_iterable())
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
        if all(self.get_binding(model)._inited for app in self.apps.values() for model in app.values()):
            return
        if self._preparation is None:
            with ModelBindingPreparation(self):
                self.init_relations()
                self.build_initial_querysets()
            return
        super().init_relations()

    def link_relations(self, *, deferred_models: frozenset[type[Model]] = frozenset()) -> None:
        uninited_models = [
            model for app in self.apps.values() for model in app.values() if not self.get_binding(model)._inited
        ]
        if not uninited_models:
            return

        models_with_missing_references: set[type[Model]] = set(deferred_models)
        # The models only a many-to-many field's target or through model of is missing - their
        # foreign keys are linked now, as their table is created with those columns.
        models_missing_only_many_to_many: set[type[Model]] = set()

        for model in uninited_models:
            binding = self.get_binding(model)
            for field_name in (
                *binding.foreign_key_fields,
                *binding.one_to_one_fields,
            ):
                foreign_key_object = binding.fields_map[field_name]
                reference = self.get_model_reference(foreign_key_object.model_name)  # type: ignore[attr-defined]
                if self._reference_is_missing(reference):
                    models_with_missing_references.add(model)
                    break  # No need to check remaining fields

            if model in models_with_missing_references:
                continue

            for field_name in binding.many_to_many_fields:
                many_to_many_object = binding.fields_map[field_name]
                references = [self.get_model_reference(many_to_many_object.model_name)]  # type: ignore[attr-defined]
                through_reference = self.get_model_reference(many_to_many_object.through_model)  # type: ignore[attr-defined]
                if through_reference is not None:
                    # A through model referenced as "app.Model" must be registered too.
                    references.append(through_reference)
                if any(self._reference_is_missing(reference) for reference in references):
                    models_with_missing_references.add(model)
                    models_missing_only_many_to_many.add(model)
                    break

        super().link_relations(deferred_models=frozenset(models_with_missing_references))

        for model in models_missing_only_many_to_many:
            RelationLinking.ensure_foreign_key_or_one_to_one_inited(self, model)

    def build_initial_querysets(self) -> None:
        """Prepare complete historical nodes with the captured base clients, without tenant routing.

        A standalone call builds another graph; nested state rendering shares its draft.
        State-only registries without connection configuration retain their structural SQL.
        """
        if self._connections._db_config is None:
            return
        if self._preparation is None:
            with ModelBindingPreparation(self):
                self.init_relations()
                self.build_initial_querysets()
            return
        for model in self.get_models_iterable():
            binding = self.get_binding(model)
            if binding.default_connection is None or not binding._inited:
                continue
            self.bind_model_base_queries(binding, self._connections.get_main(binding.default_connection))

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
            app_label = model_class._declaration.app
            if app_label is None:
                raise ConfigurationError(f"Model {model_class} is not registered in any app")
            return app_label, model_class.__name__
        return Apps.split_validated_reference(reference)

    @classmethod
    def from_model_states(
        cls,
        model_states: Mapping[tuple[str, str], ModelState],
        *,
        default_connections: Mapping[str, str],
        environment: MigrationEnvironment,
        reusable_models: Mapping[tuple[str, str], type[Model]],
    ) -> StateApps:
        """Prepare a historical registry from one version of the model declarations.

        Args:
            model_states: The states to register, in their declaration order.
            default_connections: The state's connection aliases, copied for this registry.
            environment: The captured application settings and connection owner.
            reusable_models: Unchanged class declarations. Their bindings are still rebuilt.

        Returns:
            The registry with linked relations and prepared base queries.
        """
        state_apps = cls(default_connections=dict(default_connections), environment=environment)
        with ModelBindingPreparation(state_apps):
            for model_key in model_states:
                model = reusable_models.get(model_key)
                if model is None:
                    model = model_states[model_key].render()
                state_apps.register_model(model_key[0], model)
            state_apps.init_relations()
            state_apps.build_initial_querysets()
        return state_apps
