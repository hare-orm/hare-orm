from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.state.state import State

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class FieldAddRemoveOperation(ModelBoundOperation):
    """What ``AddField`` and ``RemoveField`` share: adding and dropping a field's column, the field
    looked up by name in the target state.
    """

    name: str

    async def _add_field_to_db(
        self,
        old_state: State,
        new_state: State,
        app_label: str,
        state_editor: BaseSchemaEditor,
        *,
        foreign_key_not_valid: bool = False,
    ) -> None:
        await state_editor.add_field(
            self._model(old_state, app_label),
            self._model(new_state, app_label),
            self.name,
            foreign_key_not_valid=foreign_key_not_valid,
        )

    async def _remove_field_from_db(
        self, old_state: State, new_state: State, app_label: str, state_editor: BaseSchemaEditor
    ) -> None:
        old_model = self._model(old_state, app_label)
        await state_editor.remove_field(
            old_model, self._model(new_state, app_label), old_model._meta.fields_map[self.name]
        )
