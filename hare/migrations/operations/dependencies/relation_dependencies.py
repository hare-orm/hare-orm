from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.state.state_apps import StateApps

if TYPE_CHECKING:
    from hare.migrations.state.state import State


class RelationDependencies:
    """The dependencies relations give operations: a relation needs the model it points at, the fields
    its ``to_field`` names and its through model created; a model or field it points at is removed only
    once it no longer does."""

    @staticmethod
    def get_target_key(reference: Any, app_label: str) -> tuple[str, str] | None:
        """The (app, model) key a model reference names - ``"app.Model"``, ``"Model"`` of the app, a model
        class or ``swappable()``.

        Args:
            reference: The reference.
            app_label: The app a bare model name belongs to.

        Returns:
            The key; None for a reference naming no model now (a swappable setting not set).
        """
        if reference is None:
            return None
        if isinstance(reference, str) and "." not in reference:
            return (app_label, reference)
        try:
            reference = SwappableModelReference.get_model_reference(reference)
            if reference is None:
                return None
            return StateApps.split_reference(reference)
        except ConfigurationError:
            return None

    @classmethod
    def get_required_for_relation(cls, app_label: str, model_name: str, field: Any) -> list[OperationDependency]:
        """What a relation field needs before it's created or changed: the model it points at (but its own),
        the fields its ``to_field`` names, its through model.

        Args:
            app_label: The relation's app.
            model_name: The relation's model.
            field: The field - none needed for one that isn't a relation.

        Returns:
            The dependencies.
        """
        if not isinstance(field, (ForeignKeyFieldInstance, ManyToManyFieldInstance)):
            return []
        model_key = (app_label, model_name.lower())
        dependencies: list[OperationDependency] = []
        target_key = cls.get_target_key(field.model_name, app_label)
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
            through_key = cls.get_target_key(field.through_model, app_label)
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
        for model_key, model_state in state.models.items():
            if (model_key[0], model_key[1].lower()) == target_key:
                continue
            for field_name, field in model_state.fields.items():
                if not isinstance(field, (ForeignKeyFieldInstance, ManyToManyFieldInstance)):
                    continue
                keys = [cls.get_target_key(field.model_name, model_key[0])]
                if isinstance(field, ManyToManyFieldInstance):
                    keys.append(cls.get_target_key(field.through_model, model_key[0]))
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
                key = cls.get_target_key(field.through_model, model_key[0])
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
                key = cls.get_target_key(field.model_name, model_key[0])
                if key is None or (key[0], key[1].lower()) != target_key:
                    continue
                to_field = field.to_field
                to_field_names = (to_field,) if isinstance(to_field, str) else tuple(to_field or ())
                if field_name in to_field_names:
                    relations.append((model_key, relation_name))
        return relations
