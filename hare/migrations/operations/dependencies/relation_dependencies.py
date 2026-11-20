from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.schema_references import SchemaReferences
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.constants import RELATION_FIELDS
from hare.migrations.operations.dependencies.native_dependencies import NativeDependencies
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType

if TYPE_CHECKING:
    from hare.migrations.operations.fields.field_like import FieldLike
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.state import State


class RelationDependencies:
    """The dependencies relations give operations: a relation needs the model it points at, the fields
    its ``to_field`` names and its through model created; a model or field it points at is removed only
    once it no longer does."""

    @staticmethod
    def get_declared_operation_fields(app_label: str, operation: Operation) -> tuple[str, tuple[FieldLike, ...]]:
        """Return the owner and fields declared by a model or field operation."""
        from hare.migrations.operations.fields.add_field import AddField
        from hare.migrations.operations.fields.alter_field import AlterField
        from hare.migrations.operations.models.create_model import CreateModel
        from hare.migrations.operations.special.app_operation import AppOperation

        app_label, operation = AppOperation.unwrap(app_label, operation)
        if isinstance(operation, CreateModel):
            return app_label, tuple(field for _, field in operation.fields)
        if isinstance(operation, (AddField, AlterField)):
            return app_label, (operation.field,)
        return app_label, ()

    @staticmethod
    def get_target_key(reference: Any, app_label: str, state: State) -> tuple[str, str] | None:
        """The (app, model) key a model reference names - ``"app.Model"``, ``"Model"`` of the app, a model
        class or ``swappable()``.

        Args:
            reference: The reference.
            app_label: The app a bare model name belongs to.
            state: The captured application state selecting swappable models.

        Returns:
            The key; None for a reference naming no model now (a swappable setting not set).
        """
        if reference is None:
            return None
        if isinstance(reference, str) and "." not in reference:
            return (app_label, reference)
        try:
            return state.get_model_key(reference)
        except ConfigurationError:
            return None

    @classmethod
    def get_field_attribute_names(cls, app_label: str, name: str, field: Any, state: State) -> frozenset[str]:
        """Read implicit target keys from this state through ordinary historical relation linking."""
        targets = None
        if isinstance(field, ForeignKeyFieldInstance) and not field.to_field:
            target = cls.get_target_key(field.model_name, app_label, state)
            if target is not None and (target in state.models or target in state.environment.unmanaged_models):
                apps = state.apps
                # An added relation can be the first reference to a captured unmanaged model.
                apps._reference_is_loadable(*target)
                binding = apps.get_binding(f"{target[0]}.{target[1]}")
                targets = binding.primary_key_attribute_names
            else:
                source = field.model_name if isinstance(field.model_name, type) else None
                captured = state.environment.source_bindings.get(source) if source is not None else None
                if captured is not None:
                    targets = captured.primary_key_attribute_names
                elif source is not None:
                    declared = source._declaration
                    names = declared.primary_key_attribute
                    targets = names if isinstance(names, tuple) else (names,)
                    if any(isinstance(declared.fields_map.get(part), ForeignKeyFieldInstance) for part in targets):
                        raise ConfigurationError(
                            "A composite relation dependency needs a complete captured target state"
                        )
        return SchemaReferences.get_field_attribute_names(name, field, target_key_names=targets)

    @classmethod
    def get_field_attributes(cls, app_label: str, fields: dict[str, Any], state: State) -> dict[str, frozenset[str]]:
        """Attributes provided by every field with one authoritative target-key interpretation."""
        return {name: cls.get_field_attribute_names(app_label, name, field, state) for name, field in fields.items()}

    @classmethod
    def get_required_for_field(
        cls, app_label: str, model_name: str, field: Any, state: State
    ) -> list[OperationDependency]:
        """The containing model, generated inputs, relation targets and declared enum type."""
        dependencies = [
            OperationDependency.of_model(app_label, model_name, DependencyType.CREATED),
            *cls.get_required_for_relation(app_label, model_name, field, state),
            *(
                OperationDependency.of_field(app_label, model_name, name, DependencyType.CREATED)
                for name in field.get_generated_from_field_names()
            ),
        ]
        dependencies.extend(NativeDependencies.get_required({field.model_field_name: field}))
        return dependencies

    @classmethod
    def get_provided_for_field(
        cls, app_label: str, model_name: str, name: str, field: Any, state: State
    ) -> list[OperationDependency]:
        """A created field and every logical key attribute created with its relation."""
        return [
            OperationDependency.of_field(app_label, model_name, attribute, DependencyType.CREATED)
            for attribute in sorted(cls.get_field_attribute_names(app_label, name, field, state))
        ]

    @classmethod
    def get_required_for_relation(
        cls, app_label: str, model_name: str, field: Any, state: State
    ) -> list[OperationDependency]:
        """What a relation field needs before it's created or changed: the model it points at (but its own),
        the fields its ``to_field`` names, its through model.

        Args:
            app_label: The relation's app.
            model_name: The relation's model.
            field: The field - none needed for one that isn't a relation.
            state: The captured application state selecting swappable models.

        Returns:
            The dependencies.
        """
        if not isinstance(field, RELATION_FIELDS):
            return []
        model_key = (app_label, model_name.lower())
        dependencies: list[OperationDependency] = []
        target_key = cls.get_target_key(field.model_name, app_label, state)
        if target_key is not None:
            if (target_key[0], target_key[1].lower()) != model_key:
                dependencies.append(OperationDependency.of_model(*target_key, DependencyType.CREATED))
            to_field = field.to_field if isinstance(field, ForeignKeyFieldInstance) else None
            to_field_names = (to_field,) if isinstance(to_field, str) else tuple(to_field or ())
            dependencies.extend(
                OperationDependency.of_field(*target_key, to_field_name, DependencyType.CREATED)
                for to_field_name in to_field_names
            )
        if isinstance(field, ManyToManyFieldInstance) and field.through_model is not None:
            through_key = cls.get_target_key(field.through_model, app_label, state)
            if through_key is not None and (through_key[0], through_key[1].lower()) != model_key:
                dependencies.append(OperationDependency.of_model(*through_key, DependencyType.CREATED))
        return dependencies

    @classmethod
    def get_relations_to(cls, state: State, app_label: str, model_name: str) -> list[tuple[tuple[str, str], str, Any]]:
        """The relations of the state's other models pointing at a model, or going through it.

        Args:
            state: The state.
            app_label: The model's app.
            model_name: The model.

        Returns:
            ``(model key, field name, field)`` of each.
        """
        target_key = (app_label, model_name.lower())
        relations: list[tuple[tuple[str, str], str, Any]] = []
        for model_key in state.models:
            if (model_key[0], model_key[1].lower()) == target_key:
                continue
            model_state = state.models[model_key]
            for field_name, field in model_state.fields.items():
                if not isinstance(field, RELATION_FIELDS):
                    continue
                keys = [cls.get_target_key(field.model_name, model_key[0], state)]
                if isinstance(field, ManyToManyFieldInstance):
                    keys.append(cls.get_target_key(field.through_model, model_key[0], state))
                if any(key is not None and (key[0], key[1].lower()) == target_key for key in keys):
                    relations.append((model_key, field_name, field))
        return relations

    @classmethod
    def get_relations_through(cls, state: State, app_label: str, model_name: str) -> list[tuple[tuple[str, str], str]]:
        """The many-to-many relations of the state going through a model.

        Args:
            state: The state.
            app_label: The through model's app.
            model_name: The through model.

        Returns:
            ``(model key, field name)`` of each.
        """
        through_key = (app_label, model_name.lower())
        relations: list[tuple[tuple[str, str], str]] = []
        for model_key, model_state in state.models.items():
            for field_name, field in model_state.fields.items():
                if not isinstance(field, ManyToManyFieldInstance):
                    continue
                key = cls.get_target_key(field.through_model, model_key[0], state)
                if key is not None and (key[0], key[1].lower()) == through_key:
                    relations.append((model_key, field_name))
        return relations

    @classmethod
    def get_to_field_relations(
        cls, state: State, app_label: str, model_name: str, field_name: str
    ) -> list[tuple[tuple[str, str], str]]:
        """The relations of the state pointing at a field of a model through ``to_field``.

        Args:
            state: The state.
            app_label: The model's app.
            model_name: The model.
            field_name: The field.

        Returns:
            ``(model key, field name)`` of each.
        """
        target_key = (app_label, model_name.lower())
        relations: list[tuple[tuple[str, str], str]] = []
        for model_key, model_state in state.models.items():
            for relation_name, field in model_state.fields.items():
                if not isinstance(field, ForeignKeyFieldInstance):
                    continue
                key = cls.get_target_key(field.model_name, model_key[0], state)
                if key is None or (key[0], key[1].lower()) != target_key:
                    continue
                to_field = field.to_field
                to_field_names = (to_field,) if isinstance(to_field, str) else tuple(to_field or ())
                if field_name in to_field_names:
                    relations.append((model_key, relation_name))
        return relations
