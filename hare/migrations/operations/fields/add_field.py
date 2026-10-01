from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from hare.fields import Field
from hare.migrations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.fields.field_add_remove_operation import FieldAddRemoveOperation
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.state.project.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class AddField(FieldAddRemoveOperation):
    reads_old_state = False

    def __init__(self, model_name: str, name: str, field: FieldLike) -> None:
        self.model_name = model_name
        self.name = name
        self.field = field

    def describe(self) -> str:
        return f"Add field {self.name} to {self.model_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)

        if self.name in model_state.fields:
            raise IncompatibleStateError(f"Field {self.name} already present on model {app_label}.{self.model_name}")

        model_state.set_field(self.name, cast("Field[Any]", deepcopy(self.field)))
        models_to_reload = {(app_label, self.model_name)}

        if isinstance(self.field, DIRECT_RELATION_FIELDS):
            models_to_reload.add(state.apps.split_reference(self.field.model_name))

        state.reload_models(models_to_reload)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        await self._add_field_to_db(new_state, app_label, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        await self._remove_field_from_db(old_state, app_label, state_editor)
