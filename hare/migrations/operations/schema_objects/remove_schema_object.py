from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import ConfigurationError
from hare.migrations.operations.fields.add_field import AddField
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.operations.operation import Operation
from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
    from hare.migrations.state.state import State


class RemoveSchemaObject(ModelBoundOperation):
    """Removes a named object - an index, a constraint, a trigger - from a model, given by its
    name or, for an unnamed one, by its fields.

    Args:
        model_name: The model.
        name: The object's name.
        fields: The fields of an unnamed object.

    Raises:
        ConfigurationError: Neither ``name`` nor ``fields`` is given.
    """

    object_type: ClassVar[type[SchemaObjectType]]

    def __init__(self, model_name: str, name: str | None = None, fields: list[str] | None = None) -> None:
        if not name and not fields:
            raise ConfigurationError(f"{type(self).__name__} requires name or fields.")
        self.model_name = model_name
        self.name = name
        self.fields = fields

    def reaches_other_models(self) -> bool:
        """An object of a type running SQL against any table - a trigger - may touch any model."""
        return self.object_type.reaches_other_tables

    def get_known_removed_object(self) -> Any:
        """The very object the operation removes, when it was made knowing it - None for an
        operation naming its object, which only a state tells."""
        return None

    def reduce(self, other: Operation, app_label: str) -> list[Operation] | bool:
        """An operation knowing the object it removes cancels out with that object added back,
        and lets another field of the model be added across it."""
        removed_object = self.get_known_removed_object()
        if removed_object is not None:
            if (
                isinstance(other, AddSchemaObject)
                and other.object_type is self.object_type
                and self.is_same_model_name(self.model_name, other.model_name)
                and other.schema_object == removed_object
            ):
                return []
            if (
                isinstance(other, AddField)
                and self.is_same_model_name(self.model_name, other.model_name)
                and other.name not in (self.fields or ())
            ):
                return True
        return super().reduce(other, app_label)

    def get_type_options(self) -> dict[str, Any]:
        """The type's options of this operation, handed to the schema editor - none by default."""
        return {}

    def check_can_run(self, state_editor: BaseSchemaEditor) -> None:
        """Refuses running the way this operation can't - nothing by default.

        Args:
            state_editor: The migration's schema editor.
        """

    def describe(self) -> str:
        action = "Concurrently remove" if self.get_type_options().get("concurrently") else "Remove"
        described = self.name or (f"on {', '.join(self.fields)}" if self.fields else "")
        return f"{action} {self.object_type.noun} {described} from {self.model_name}"

    def find(self, state: State, app_label: str) -> Any:
        """The removed object, as the state stores it.

        Args:
            state: A state holding it.
            app_label: The migration's app.

        Returns:
            The object.
        """
        model_state = self.get_model_state(state, app_label, self.model_name)
        return self.object_type.find(
            model_state, lambda: self._model(state, app_label), name=self.name, fields=self.fields
        )

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)
        self.object_type.update_objects(model_state, removed=self.find(state, app_label))
        state.reload_model(app_label, self.model_name)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        self.check_can_run(state_editor)
        model = self._model(old_state, app_label)
        await self.object_type.remove(state_editor, model, self.find(old_state, app_label), **self.get_type_options())

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        self.check_can_run(state_editor)
        model = self._model(new_state, app_label)
        await self.object_type.add(state_editor, model, self.find(new_state, app_label), **self.get_type_options())
