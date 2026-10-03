from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast

from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.swappable import SwappableModelReference
from hare.migrations.exceptions import InconsistentMigrationStateError
from hare.migrations.reports.operation_plan import OperationPlan
from hare.migrations.state.apps import StateApps
from hare.migrations.state.project.model_state import ModelState
from hare.models import Model
from hare.models.enums import ModelOption


@dataclass
class State:
    models: dict[tuple[str, str], ModelState]
    apps: StateApps
    #: Leave a relation declared with ``swappable()`` unresolved without failing - makemigrations
    #: replays a package's migrations before the app its setting points into has any.
    ignores_missing_swappable_relations: bool = False

    @classmethod
    def from_models(
        cls,
        models: Iterable[type[Model]],
        *,
        app_label: str | None = None,
        default_connections: dict[str, str] | None = None,
    ) -> State:
        """The state of model classes as they are declared - for a migration built in memory, the
        models its operations relate to, or the new version of the models it brings.

        A table whose schema an application changes while it runs (``Meta.managed = False``, so
        migration files leave it alone) gets its current state from its own migrations applied
        to a state - ``Migration.apply(state)`` without a schema editor - not from its
        registered class.

        Args:
            models: The model classes.
            app_label: The app of a model that names none (``Meta.app``) and isn't registered.
            default_connections: Each app's default connection name.

        Returns:
            The state.

        Raises:
            ConfigurationError: A model has no app and no ``app_label`` is given.
        """
        model_states: dict[tuple[str, str], ModelState] = {}
        for model in models:
            model_app_label = model._meta.app or app_label
            if model_app_label is None:
                raise ConfigurationError(f"Model {model.__name__} names no app - pass app_label")
            model_states[(model_app_label, model.__name__)] = ModelState.make_from_model(model_app_label, model)
        state_apps = StateApps(default_connections=dict(default_connections or {}))
        return cls(models=model_states, apps=state_apps.clone(model_states=model_states))

    def get_operations(self, new_state: State, app_label: str) -> OperationPlan:
        """The operations that bring one app's models from this state to another - what
        ``makemigrations`` writes for the app, with its warnings.

        Args:
            new_state: The state to reach.
            app_label: The app.

        Returns:
            The operations, the warnings and the warnings of what the change can lose.
        """
        # Deferred: the operation generator imports the operations, which import this module.
        from hare.migrations.autodetection.operation_generator import OperationGenerator

        generator = OperationGenerator(self, new_state)
        operations = generator.generate(app_labels=[app_label])
        return OperationPlan(
            operations=tuple(operations),
            warnings=tuple(generator.warnings),
            data_loss_warnings=tuple(generator.data_loss_warnings),
        )

    @staticmethod
    def _get_related_models(model: type[Model]) -> list[type[Model]]:
        """Returns the models directly reachable from `model` via subclassing or a relational
        field - one recursion step for computing every model transitively affected by a change."""
        related_models = [subclass for subclass in model.__subclasses__() if issubclass(subclass, Model)]

        for field_name in model._meta.fetch_fields:
            field = cast("RelationalField[Model]", model._meta.fields_map[field_name])
            # related_model may be None if the target model hasn't been registered yet
            # (e.g. during migration state-building when CreateModel operations are
            # processed in alphabetical order).
            if field.related_model is not None:
                related_models.append(field.related_model)

        return related_models

    @staticmethod
    def _require_app_label(model: type[Model]) -> str:
        app_label = model._meta.app
        if app_label is None:
            raise ConfigurationError(f"Model {model} is not registered in any app")
        return app_label

    @classmethod
    def _get_related_models_recursive(cls, model: type[Model]) -> set[tuple[str, str]]:
        """Returns every (app_label, model_name) transitively reachable from `model` via
        subclassing or a relational field, excluding `model` itself."""
        seen: set[tuple[str, str]] = set()
        rel_models = cls._get_related_models(model)

        for rel_model in rel_models:
            if rel_model._meta.app is None:
                continue
            model_tuple = (cls._require_app_label(rel_model), rel_model.__name__)
            if model_tuple in seen:
                continue
            seen.add(model_tuple)
            rel_models += cls._get_related_models(rel_model)

        return seen - {(cls._require_app_label(model), model.__name__)}

    def _find_related_models(self, app_label: str, model_name: str) -> set[tuple[str, str]]:
        try:
            model = self.apps.get_model(f"{app_label}.{model_name}")
        except KeyError:
            related_models: set[tuple[str, str]] = set()
        else:
            related_models = self._get_related_models_recursive(model)

        related_models.add((app_label, model_name))

        return related_models

    def _reload(self, models_to_reload: set[tuple[str, str]]) -> None:
        for app_label, model_name in models_to_reload:
            self.apps.unregister_model(app_label, model_name)
            model_state = self.models[(app_label, model_name)]
            model = model_state.render(self.apps)
            self.apps.register_model(app_label, model)

        self.apps._init_relations()
        self.apps._build_initial_querysets()
        for app_label, model_name in models_to_reload:
            model = self.apps.get_model(f"{app_label}.{model_name}")
            if not model._meta._inited:
                continue
            # A migration file keeps an index's key expressions as declared; resolved against the
            # rendered model here, as ModelState.make_from_model() resolves a live model's, so the
            # index's generated name and its comparisons work the same on both sides.
            for index in model._meta.indexes:
                if isinstance(index, Index) and index.declared_expressions:
                    index.get_expressions(model)

    def add_rendered_model(self, app_label: str, model_name: str) -> None:
        """Renders a model just added to the state into the rendered apps and links it there in
        place, as ``Apps`` links models when it starts - no model is rendered again. A model its
        relations reach gets the other side of the relation added and its base queries built
        again, as does a model waiting for it to be added.

        Args:
            app_label: The model's app.
            model_name: The model's name.
        """
        model = self.models[(app_label, model_name)].render(self.apps)
        self.apps.register_model(app_label, model)
        uninited_models = [
            app_model for app in self.apps.apps.values() for app_model in app.values() if not app_model._meta._inited
        ]
        # Only the models this one links to are finalised - not every rendered model again.
        self.apps._link_relations()
        linked_models: dict[type[Model], None] = {}
        for linked_model in uninited_models:
            if not linked_model._meta._inited:
                continue
            linked_models[linked_model] = None
            for field_name in linked_model._meta.fetch_fields:
                related_model = cast("RelationalField[Model]", linked_model._meta.fields_map[field_name]).related_model
                if related_model is not None:
                    linked_models[related_model] = None
        for linked_model in linked_models:
            linked_model._meta.finalise_model()
        self.apps.build_querysets(linked_models)
        for linked_model in linked_models:
            # An index's declared key expressions resolved against the rendered model.
            for index in linked_model._meta.indexes:
                if isinstance(index, Index) and index.declared_expressions:
                    index.get_expressions(linked_model)

    def reload_model(self, app_label: str, model_name: str) -> None:
        model_state = self.models.get((app_label, model_name))
        if not model_state:
            raise LookupError(f"Model state {app_label}.{model_name} is unknown")

        related_models = self._find_related_models(app_label, model_name)
        self._reload(related_models)

    def reload_models(self, model_tuples: Iterable[tuple[str, str]]) -> None:
        related_models: set[tuple[str, str]] = set()

        for app_label, model_name in model_tuples:
            model_state = self.models.get((app_label, model_name))
            if not model_state:
                continue

            related_models |= self._find_related_models(app_label, model_name)

        self._reload(related_models)

    def validate_relations_initialized(self) -> None:
        """Raises when a model's relations weren't set up - a relation target was never resolved.

        Raises:
            InconsistentMigrationStateError: A model is still not inited.
        """
        missing_references: list[str] = []
        for app in self.apps.apps.values():
            for model in app.values():
                if model._meta._inited:
                    continue
                missing_relation_targets = self._get_missing_relation_targets(model)
                if (
                    self.ignores_missing_swappable_relations
                    and missing_relation_targets
                    and all(
                        isinstance(
                            getattr(model._meta.fields_map[field_name], "model_name", None), SwappableModelReference
                        )
                        for field_name, _target in missing_relation_targets
                    )
                ):
                    continue
                model_reference = f"{model._meta.app}.{model.__name__}"
                missing_targets = [
                    f"{model_reference}.{field_name} -> {target}" for field_name, target in missing_relation_targets
                ]
                missing_references.extend(missing_targets or [model_reference])
        if missing_references:
            raise InconsistentMigrationStateError(
                "The migration files can't be replayed - uninitialized relations remain after "
                "applying them, pointing at a model no migration creates (or one a migration "
                f"already deleted): {', '.join(sorted(missing_references))}. Make the migration removing or "
                "repointing each relation depend on (run before) the migration deleting its "
                "target, or restore the missing CreateModel."
            )

    def _get_missing_relation_targets(self, model: type[Model]) -> list[tuple[str, str]]:
        """The relation fields of `model` whose "app.Model" target isn't part of this state.

        Args:
            model: A rendered state model.

        Returns:
            (field name, target reference) pairs.
        """
        missing_targets_by_field: list[tuple[str, str]] = []
        for field_name, field in model._meta.fields_map.items():
            if not isinstance(field, (ForeignKeyFieldInstance, ManyToManyFieldInstance)) or getattr(
                field, "_generated", False
            ):
                continue
            references: list[object] = [SwappableModelReference.get_model_reference(field.model_name)]
            if isinstance(field, ManyToManyFieldInstance):
                references.append(SwappableModelReference.get_model_reference(field.through_model))
            for reference in references:
                if not isinstance(reference, str) or "." not in reference:
                    continue
                target_app, target_name = reference.split(".", 1)
                if target_name not in self.apps.apps.get(target_app, {}):
                    missing_targets_by_field.append((field_name, reference))
        return missing_targets_by_field

    def get_models_with_tables(self) -> list[type[Model]]:
        """Every model of this state except the ones swapped for another model by their
        ``swappable`` setting, which have no table."""
        return [
            model
            for app_label, app in self.apps.apps.items()
            for model_name, model in app.items()
            if not self.is_swapped_model(app_label, model_name)
        ]

    def is_swapped_model(self, app_label: str, model_name: str) -> bool:
        """Whether a model of this state is swapped for another one by its ``swappable`` setting -
        it has no table, so no operation touches one for it.

        Args:
            app_label: The model's app.
            model_name: The model's name.
        """
        model_state = self.models.get((app_label, model_name))
        setting = model_state.options.get(ModelOption.SWAPPABLE) if model_state is not None else None
        if not setting:
            return False
        return self.apps.get_swappable_label(setting) != f"{app_label}.{model_name}"

    def clone(self) -> State:
        models = {key: model.clone() for key, model in self.models.items()}
        return self.__class__(
            models=models,
            apps=self.apps.clone(model_states=models),
            ignores_missing_swappable_relations=self.ignores_missing_swappable_relations,
        )
