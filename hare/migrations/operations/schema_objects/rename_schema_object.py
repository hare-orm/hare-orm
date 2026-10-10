from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import ConfigurationError
from hare.migrations.operations.model_bound_operation import ModelBoundOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
    from hare.migrations.state.state import State


class RenameSchemaObject(ModelBoundOperation):
    """Renames a named object - an index, a constraint, a trigger - of a model, given by its old
    name or, for an unnamed index, by its fields.

    Args:
        model_name: The model.
        new_name: The new name.
        old_name: The old name.
        old_fields: The fields of an unnamed object.

    Raises:
        ConfigurationError: Neither or both of ``old_name`` and ``old_fields`` are given.
    """

    object_type: ClassVar[type[SchemaObjectType]]

    def __init__(
        self, model_name: str, new_name: str, *, old_name: str | None = None, old_fields: list[str] | None = None
    ) -> None:
        if not old_name and not old_fields:
            raise ConfigurationError(f"{type(self).__name__} requires old_name or old_fields.")
        if old_name and old_fields:
            raise ConfigurationError(f"{type(self).__name__}.old_name and old_fields are mutually exclusive.")
        self.model_name = model_name
        self.new_name = new_name
        self.old_name = old_name
        self.old_fields = old_fields

    def reaches_other_models(self) -> bool:
        """An object of a type running SQL against any table - a trigger - may touch any model."""
        return self.object_type.reaches_other_tables

    def describe(self) -> str:
        old = self.old_name or f"on {', '.join(self.old_fields or ())}"
        return f"Rename {self.object_type.noun} {old} to {self.new_name} on {self.model_name}"

    def find_old(self, state: State, app_label: str) -> Any:
        """The object under its old name, as the state stores it.

        Args:
            state: A state holding it under its old name.
            app_label: The migration's app.

        Returns:
            The object.
        """
        model_state = self.get_model_state(state, app_label, self.model_name)
        return self.object_type.find_for_rename(
            model_state, lambda: self._model(state, app_label), name=self.old_name, fields=self.old_fields
        )

    def find_new(self, state: State, app_label: str) -> Any:
        """The object under its new name.

        Args:
            state: A state holding it under its new name.
            app_label: The migration's app.

        Returns:
            The object.
        """
        model_state = self.get_model_state(state, app_label, self.model_name)
        return self.object_type.find(model_state, lambda: self._model(state, app_label), name=self.new_name)

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)
        old_object = self.find_old(state, app_label)
        self.object_type.update_objects(
            model_state, removed=old_object, added=self.object_type.renamed(old_object, self.new_name)
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
        await self.object_type.rename(
            state_editor, model, self.find_old(old_state, app_label), self.find_new(new_state, app_label)
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
        # Going back, old_state is the renamed state and new_state the one before the rename.
        model = self._model(old_state, app_label)
        await self.object_type.rename(
            state_editor, model, self.find_new(old_state, app_label), self.find_old(new_state, app_label)
        )
