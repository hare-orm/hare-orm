from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.model_bound_operation import ModelBoundOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
    from hare.migrations.state.state import State


class AddSchemaObject(ModelBoundOperation):
    """Adds a named object - an index, a constraint, a trigger - to a model; a named subclass per
    type (``AddIndex``, ``AddConstraint``, ``AddTrigger``) gives its argument its own name."""

    object_type: ClassVar[type[SchemaObjectType]]
    reads_old_state = False

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def reaches_other_models(self) -> bool:
        """An object of a type running SQL against any table - a trigger - may touch any model."""
        return self.object_type.reaches_other_tables

    @property
    @abc.abstractmethod
    def schema_object(self) -> Any:
        """The object added."""

    def get_type_options(self) -> dict[str, Any]:
        """The type's options of this operation, handed to the schema editor - none by default."""
        return {}

    def check_can_run(self, state_editor: BaseSchemaEditor) -> None:
        """Refuses running the way this operation can't - nothing by default.

        Args:
            state_editor: The migration's schema editor.
        """

    def describe(self) -> str:
        return (
            f"{self.get_description_action()} {self.object_type.noun}{self.get_described_name()} to {self.model_name}"
        )

    def get_description_action(self) -> str:
        """The verb ``describe()`` starts with."""
        return "Add"

    def get_described_name(self) -> str:
        """The object's name as ``describe()`` writes it - with a leading space; nothing for an
        unnamed object."""
        name = self.object_type.get_name(self.schema_object)
        return f" {name}" if name else ""

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)
        self.object_type.update_objects(model_state, added=self.object_type.get_state_entry(self.schema_object))
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
        await self.object_type.add(
            state_editor, self._model(new_state, app_label), self.schema_object, **self.get_type_options()
        )

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
        await self.object_type.remove(
            state_editor, self._model(old_state, app_label), self.schema_object, **self.get_type_options()
        )
