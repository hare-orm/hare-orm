from __future__ import annotations

import dataclasses
from collections.abc import Iterable, MutableMapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.core.apps.swappable_models import SwappableModels
from hare.ddl.schema_objects.enum_type import EnumType
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.constants import RELATION_FIELDS
from hare.migrations.exceptions import InconsistentMigrationStateError
from hare.migrations.reports.operation_plan import OperationPlan
from hare.migrations.state.migration_environment import MigrationEnvironment
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state_apps import StateApps
from hare.migrations.state.state_rendering import StateRendering
from hare.models import Model
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.migrations.autodetection.rename_questions import RenameQuestions
    from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
    from hare.migrations.operations.operation import Operation
    from hare.models.model_binding import ModelBinding


@dataclass
class State:
    """The schema a run of migrations leaves: the models, and the extensions, schemas and ``ENUM``
    types the operations created. The model classes (``apps``) are rendered from it only when an
    operation asks for them."""

    models: MutableMapping[tuple[str, str], ModelState] = dataclasses.field(default_factory=dict)
    #: Each app's default connection alias.
    default_connections: dict[str, str] = dataclasses.field(default_factory=dict)
    #: Leave a relation declared with ``swappable()`` unresolved without failing - makemigrations
    #: replays a package's migrations before the app its setting points into has any.
    ignores_missing_swappable_relations: bool = False
    #: The extensions the operations created, by database alias and name.
    extensions: set[tuple[str | None, str]] = dataclasses.field(default_factory=set)
    #: The schemas the operations created, by database alias and name.
    schemas: set[tuple[str | None, str]] = dataclasses.field(default_factory=set)
    #: The ``ENUM`` types the operations created, by database alias, schema and name.
    enum_types: dict[tuple[str | None, str | None, str], EnumType] = dataclasses.field(default_factory=dict)
    #: The app whose migration provides the current native resource definition.
    native_providers: dict[OperationDependency, str] = dataclasses.field(default_factory=dict, kw_only=True)
    environment: MigrationEnvironment = dataclasses.field(
        default_factory=MigrationEnvironment.capture, repr=False, compare=False, kw_only=True
    )
    rendering: StateRendering = dataclasses.field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.rendering = StateRendering(self.default_connections, self.environment)

    def record_native_operation(self, app_label: str, operation: Operation) -> None:
        """Keep durable native-resource provenance for cross-app migration dependencies."""
        # Operations import State while their package initializes.
        from hare.migrations.operations.dependencies.constants import DATABASE_DEPENDENCY_SUBJECTS
        from hare.migrations.operations.enums import DependencyType

        alias = self.default_connections.get(app_label)
        for dependency in operation.get_provided_dependencies(app_label, self):
            if dependency.subject not in DATABASE_DEPENDENCY_SUBJECTS:
                continue
            resource = dataclasses.replace(dependency, dependency_type=DependencyType.CREATED, database_alias=alias)
            if dependency.dependency_type is DependencyType.REMOVED:
                self.native_providers.pop(resource, None)
            else:
                self.native_providers[resource] = app_label

    @property
    def apps(self) -> StateApps:
        """The model classes rendered from the state, up to date with every change made so far."""
        return self.rendering.get_apps(self.models)

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

    @classmethod
    def from_models(
        cls,
        models: Iterable[type[Model]],
        *,
        app_label: str | None = None,
        default_connections: dict[str, str] | None = None,
        environment: MigrationEnvironment | None = None,
    ) -> State:
        """Capture model declarations through one explicit live or offline registry graph."""
        environment = environment if environment is not None else MigrationEnvironment.capture()
        bindings = environment.get_bindings(list(models), app_label=app_label, default_connections=default_connections)
        model_states = {}
        connections = dict(default_connections or {})
        for binding in bindings:
            label = binding.app or app_label
            if label is None:
                raise ConfigurationError(f"Model {binding._model.__name__} names no app - pass app_label")
            if binding.default_connection is not None:
                connections.setdefault(label, binding.default_connection)
            model_states[(label, binding._model.__name__)] = environment.get_model_state(label, binding)
        return cls(models=model_states, default_connections=connections, environment=environment)

    def get_operations(
        self, new_state: State, app_label: str, rename_questions: RenameQuestions | None = None
    ) -> OperationPlan:
        """The operations that bring one app's models from this state to another - what
        ``makemigrations`` writes for the app, with its warnings.

        Args:
            new_state: The state to reach.
            app_label: The app.
            rename_questions: Answers for candidate renames that cannot be inferred from
                unambiguous field-column identity or relation signatures.

        Returns:
            The operations, the warnings and the warnings of what the change can lose.
        """
        # Deferred: the operation generator imports the operations, which import this module.
        from hare.migrations.autodetection.cross_app_view_changes import CrossAppViewChanges
        from hare.migrations.autodetection.operation_generator import OperationGenerator

        generator = OperationGenerator(self, new_state, rename_questions=rename_questions)
        operations = generator.generate(app_labels=[app_label])
        changes = CrossAppViewChanges(
            self, new_state, generator.connection_by_app, {app_label: generator.changed_columns}
        )
        operations_by_app = {app_label: operations} if operations else {}
        changes.prepare(operations_by_app)
        if app_label in changes.groups:
            operations = changes.surround(app_label, operations_by_app[app_label])
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
            if not isinstance(field, RELATION_FIELDS) or getattr(field, "_generated", False):
                continue
            references: list[object] = [self.get_model_reference(field.model_name)]
            if isinstance(field, ManyToManyFieldInstance):
                references.append(self.get_model_reference(field.through_model))
            for reference in references:
                if (
                    isinstance(reference, type)
                    and issubclass(reference, Model)
                    and reference._declaration.app is not None
                ):
                    reference = f"{reference._declaration.app}.{reference.__name__}"
                if not isinstance(reference, str) or "." not in reference:
                    continue
                target_key = tuple(reference.split(".", 1))
                if target_key not in self.models and target_key not in self.environment.unmanaged_models:
                    missing_targets_by_field.append((field_name, reference))
        return missing_targets_by_field

    def get_swappable_label(self, setting: str) -> str:
        """The label a ``swappable`` setting points at in the captured environment, else the
        one model of this state declaring ``Meta.swappable = setting``.

        Args:
            setting: The setting name.

        Raises:
            ConfigurationError: The setting is neither configured nor declared by exactly one model.
        """
        return self.environment.get_swappable_label(setting) or self.get_declared_swappable_label(setting)

    def get_model_reference(self, reference: Any) -> Any:
        """Bind a swappable reference using this state's captured environment."""
        if isinstance(reference, type) and reference in self.environment.source_bindings:
            binding = self.environment.source_bindings[reference]
            return f"{binding.app}.{reference.__name__}"
        return (
            self.get_swappable_label(reference.setting)
            if isinstance(reference, SwappableModelReference)
            else reference
        )

    def get_model_key(self, reference: str | type[Model] | SwappableModelReference) -> tuple[str, str]:
        """Read a model key without consulting the current application."""
        return StateApps.split_reference(self.get_model_reference(reference))

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
        return SwappableModels.get_declared_model_label(setting, labels)

    def get_bindings_with_tables(self) -> list[ModelBinding]:
        """Every rendered model of this state except the ones swapped for another model by their
        ``swappable`` setting, which have no table."""
        apps = self.apps
        return [
            apps.get_binding(model)
            for app_label, app in apps.apps.items()
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
        return self._copy_with_models({key: model.clone() for key, model in self.models.items()})

    def _copy_with_models(self, models: MutableMapping[tuple[str, str], ModelState]) -> State:
        """Copy state metadata around the supplied working model declarations."""
        return self.__class__(
            models=models,
            default_connections=dict(self.default_connections),
            ignores_missing_swappable_relations=self.ignores_missing_swappable_relations,
            extensions=set(self.extensions),
            schemas=set(self.schemas),
            enum_types=deepcopy(self.enum_types),
            native_providers=dict(self.native_providers),
            environment=self.environment,
        )
