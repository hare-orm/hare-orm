from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.model_bound_operation import ModelBoundOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
    from hare.migrations.state.state import State


class AlterSchemaObject(ModelBoundOperation):
    """Changes a named object of a model in place - its new version replaces the one of the same
    name; a named subclass per type (``AlterTrigger``) gives its argument its own name."""

    object_type: ClassVar[type[SchemaObjectType]]

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def reaches_other_models(self) -> bool:
        """An object of a type running SQL against any table - a trigger - may touch any model."""
        return self.object_type.reaches_other_tables

    @property
    @abc.abstractmethod
    def schema_object(self) -> Any:
        """The object's new version."""

    def describe(self) -> str:
        return f"Alter {self.object_type.noun} {self.object_type.get_name(self.schema_object)} on {self.model_name}"

    def find_current(self, state: State, app_label: str) -> Any:
        """The version of the object a state holds.

        Args:
            state: The state.
            app_label: The migration's app.

        Returns:
            The object.
        """
        model_state = self.get_model_state(state, app_label, self.model_name)
        return self.object_type.find(
            model_state, lambda: self._model(state, app_label), name=self.object_type.get_name(self.schema_object)
        )

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)
        self.object_type.update_objects(
            model_state,
            removed=self.find_current(state, app_label),
            added=self.object_type.get_state_entry(self.schema_object),
        )
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
        model = self._model(new_state, app_label)
        await self.object_type.alter(state_editor, model, self.find_current(old_state, app_label), self.schema_object)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        # Going back, new_state holds the version before the change.
        model = self._model(old_state, app_label)
        await self.object_type.alter(state_editor, model, self.schema_object, self.find_current(new_state, app_label))
