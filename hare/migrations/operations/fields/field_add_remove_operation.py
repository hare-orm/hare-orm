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
            self.get_model_binding(old_state, app_label),
            self.get_model_binding(new_state, app_label),
            self.name,
            foreign_key_not_valid=foreign_key_not_valid,
        )

    async def _remove_field_from_db(
        self, old_state: State, new_state: State, app_label: str, state_editor: BaseSchemaEditor | None
    ) -> None:
        if not state_editor:
            return
        old_binding = self.get_model_binding(old_state, app_label)
        await state_editor.remove_field(
            old_binding,
            self.get_model_binding(new_state, app_label),
            old_binding.fields_map[self.name],
        )
