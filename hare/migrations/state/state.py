from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.ddl.indexes.index import Index
from hare.ddl.schema_objects.enum_type import EnumType
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.exceptions import InconsistentMigrationStateError
from hare.migrations.reports.operation_plan import OperationPlan
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state_apps import StateApps
from hare.migrations.state.state_rendering import StateRendering
from hare.migrations.state.tracked_dict import TrackedDict
from hare.models import Model
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.migrations.autodetection.rename_questions import RenameQuestions
    from hare.migrations.state.declarations import OperationUndo


@dataclass
class State:
    """The schema a run of migrations leaves: the models, and the extensions, schemas and ``ENUM``
    types the operations created. The model classes (``apps``) are rendered from it only when an
    operation asks for them."""

    models: dict[tuple[str, str], ModelState] = dataclasses.field(default_factory=dict)
    #: Each app's default connection alias.
    default_connections: dict[str, str] = dataclasses.field(default_factory=dict)
    #: Leave a relation declared with ``swappable()`` unresolved without failing - makemigrations
    #: replays a package's migrations before the app its setting points into has any.
    ignores_missing_swappable_relations: bool = False
    #: The extensions the operations created.
    extensions: set[str] = dataclasses.field(default_factory=set)
    #: The schemas the operations created.
    schemas: set[str] = dataclasses.field(default_factory=set)
    #: The ``ENUM`` types the operations created, by schema and name.
    enum_types: dict[tuple[str | None, str], EnumType] = dataclasses.field(default_factory=dict)
    rendering: StateRendering = dataclasses.field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.models = TrackedDict(self.models, owner=self)
        self.rendering = StateRendering(self.default_connections)

    @property
    def apps(self) -> StateApps:
        """The model classes rendered from the state, up to date with every change made so far."""
        return self.rendering.get_apps(self.models)

    def build_index_expressions(self) -> None:
        """Builds the key expressions of the state's indexes from their declared form - a migration file
        keeps them as declared - against the rendered models, for their generated names and comparisons;
        renders the models only when an expression is still unbuilt."""
        if not any(
            isinstance(index, Index) and index.has_unresolved_expressions
            for model_state in self.models.values()
            for index in model_state.options.get(ModelOption.INDEXES, ())
        ):
            return
        apps = self.apps
        StateRendering.build_index_expressions(model for app in apps.apps.values() for model in app.values())

    def mark_model_added(self, app_label: str, model_name: str) -> None:
        """Records that a model was added to ``models`` - rendered when the classes are next asked for.

        Args:
            app_label: The model's app.
            model_name: The model's name.
        """
        self.rendering.mark_added((app_label, model_name))

    def mark_models_changed(self, model_keys: Iterable[tuple[str, str]]) -> None:
        """Records that models of ``models`` changed - rendered again, with the models they reach,
        when the classes are next asked for.

        Args:
            model_keys: The models' (app, model name) keys.
        """
        for model_key in model_keys:
            if model_key in self.models:
                self.rendering.mark_changed(model_key)

    def mark_model_removed(self, app_label: str, model_name: str) -> None:
        """Records that a model was taken out of ``models``.

        Args:
            app_label: The model's app.
            model_name: The model's name.
        """
        self.rendering.mark_removed((app_label, model_name))

    def undo(self, undo: OperationUndo) -> None:
        """Takes the state back to before an operation - its model states, extensions, schemas and enum
        types as the state change journal recorded them; the rendered classes follow when next asked for.

        Args:
            undo: What the journal recorded of the operation.
        """
        for key, model_state in undo.model_states.items():
            if model_state is None:
                if key in self.models:
                    del self.models[key]
                    self.mark_model_removed(*key)
            elif key in self.models:
                self.models[key] = model_state
                self.mark_models_changed([key])
            else:
                self.models[key] = model_state
                self.mark_model_added(*key)
        self.extensions = set(undo.extensions)
        self.schemas = set(undo.schemas)
        self.enum_types = dict(undo.enum_types)

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
            default_connections: Each app's default connection alias.

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
        return cls(models=model_states, default_connections=dict(default_connections or {}))

    def get_operations(
        self, new_state: State, app_label: str, rename_questions: RenameQuestions | None = None
    ) -> OperationPlan:
        """The operations that bring one app's models from this state to another - what
        ``makemigrations`` writes for the app, with its warnings.

        Args:
            new_state: The state to reach.
            app_label: The app.
            rename_questions: The answers whether a removed field or model is renamed - none renamed
                without them.

        Returns:
            The operations, the warnings and the warnings of what the change can lose.
        """
        # Deferred: the operation generator imports the operations, which import this module.
        from hare.migrations.autodetection.operation_generator import OperationGenerator

        generator = OperationGenerator(self, new_state, rename_questions=rename_questions)
        operations = generator.generate(app_labels=[app_label])
        return OperationPlan(
            operations=tuple(operations),
            warnings=tuple(generator.warnings),
            data_loss_warnings=tuple(generator.data_loss_warnings),
        )

    def validate_relations_initialized(self) -> None:
        """Raises when a relation's target isn't part of the state - a model no migration creates,
        or one a migration already deleted.

        Raises:
            InconsistentMigrationStateError: A relation's target is missing.
        """
        missing_references: list[str] = []
        for (app_label, model_name), model_state in self.models.items():
            missing_relation_targets = self.get_missing_relation_targets(model_state)
            if not missing_relation_targets:
                continue
            if self.ignores_missing_swappable_relations and all(
                isinstance(getattr(model_state.fields[field_name], "model_name", None), SwappableModelReference)
                for field_name, _target in missing_relation_targets
            ):
                continue
            missing_references.extend(
                f"{app_label}.{model_name}.{field_name} -> {target}" for field_name, target in missing_relation_targets
            )
        if missing_references:
            raise InconsistentMigrationStateError(
                "The migration files can't be replayed - uninitialized relations remain after "
                "applying them, pointing at a model no migration creates (or one a migration "
                f"already deleted): {', '.join(sorted(missing_references))}. Make the migration removing or "
                "repointing each relation depend on (run before) the migration deleting its "
                "target, or restore the missing CreateModel."
            )

    def get_missing_relation_targets(self, model_state: ModelState) -> list[tuple[str, str]]:
        """The relation fields of a model whose ``"app.Model"`` target - or through model - is
        neither part of this state nor a ``Meta.managed = False`` model of the running application,
        which no migration creates.

        Args:
            model_state: A model of this state.

        Returns:
            (field name, target reference) pairs.
        """
        missing_targets_by_field: list[tuple[str, str]] = []
        for field_name, field in model_state.fields.items():
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
                target_key = tuple(reference.split(".", 1))
                if target_key not in self.models and not StateApps.is_unmanaged_live_model(*target_key):
                    missing_targets_by_field.append((field_name, reference))
        return missing_targets_by_field

    def get_swappable_label(self, setting: str) -> str:
        """The ``app_label.ModelName`` label a ``swappable`` setting points at - read from the live
        app registry, else the one model of this state declaring ``Meta.swappable = setting``.

        Args:
            setting: The setting name.

        Raises:
            ConfigurationError: The setting is neither configured nor declared by exactly one model.
        """
        return StateApps.get_live_swappable_label(setting) or self.get_declared_swappable_label(setting)

    def get_declared_swappable_label(self, setting: str) -> str:
        """The label of the one model of this state declaring ``Meta.swappable = setting``.

        Args:
            setting: The setting name.

        Raises:
            ConfigurationError: No model, or several, declare it.
        """
        labels = [
            f"{app_label}.{model_name}"
            for (app_label, model_name), model_state in self.models.items()
            if model_state.options.get(ModelOption.SWAPPABLE) == setting
        ]
        if len(labels) == 1:
            return labels[0]
        if not labels:
            raise ConfigurationError(
                f"Unknown swappable setting {setting!r} - no model declares Meta.swappable = {setting!r}; "
                'set it in the "swappable" config section'
            )
        raise ConfigurationError(
            f"Swappable setting {setting!r} is declared by several models ({', '.join(labels)}) - "
            'choose one in the "swappable" config section'
        )

    def get_models_with_tables(self) -> list[type[Model]]:
        """Every rendered model of this state except the ones swapped for another model by their
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
        return self.get_swappable_label(setting) != f"{app_label}.{model_name}"

    def is_tenant_schema_model(self, app_label: str, model_name: str) -> bool:
        """Whether a model of this state has ``Meta.tenant_schema`` - its table is in each tenant's
        schema.

        Args:
            app_label: The model's app.
            model_name: The model's name.
        """
        model_state = self.models.get((app_label, model_name))
        return model_state is not None and bool(model_state.options.get(ModelOption.TENANT_SCHEMA, False))

    def clone(self) -> State:
        """A copy to change on its own - its classes rendered only when asked for; this state keeps
        its own rendering."""
        return self.__class__(
            models={key: model.clone() for key, model in self.models.items()},
            default_connections=dict(self.default_connections),
            ignores_missing_swappable_relations=self.ignores_missing_swappable_relations,
            extensions=set(self.extensions),
            schemas=set(self.schemas),
            enum_types=dict(self.enum_types),
        )
