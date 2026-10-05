from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from hare.fields import Field
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.operations.fields.remove_field import RemoveField
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.operations.operation import Operation
from hare.migrations.reports.operation_effect import OperationEffect
from hare.migrations.state.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class AlterField(ModelBoundOperation):
    """Changes the definition of a model's field."""

    def __init__(self, model_name: str, name: str, field: FieldLike) -> None:
        self.model_name = model_name
        self.name = name
        self.field = field

    def get_fields(self) -> list[Any]:
        """The field's new definition."""
        return [self.field]

    def reduce(self, other: Operation, app_label: str) -> list[Operation] | bool:
        """A later change or removal of the same field takes this one's place."""
        if (
            isinstance(other, (AlterField, RemoveField))
            and self.is_same_model_name(self.model_name, other.model_name)
            and other.name == self.name
        ):
            return [other]
        return super().reduce(other, app_label)

    def describe(self) -> str:
        return f"Alter field {self.name} on {self.model_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)

        if self.name not in model_state.fields:
            raise IncompatibleStateError(f"Field {self.name} is not present on model {app_label}.{self.model_name}")

        old_field = model_state.fields[self.name]
        model_state.set_field(self.name, cast("Field[Any]", deepcopy(self.field)))
        model_state.sync_field_index(self.name, old_field, model_state.fields[self.name])
        state.reload_models(self.get_models_to_reload(app_label, state, self.field))

    def get_effect(self, app_label: str, state: State, dialect: Dialect) -> OperationEffect:
        new_state = state.clone()
        self.state_forward(app_label, new_state)
        old_model = self._model(state, app_label)
        new_model = self._model(new_state, app_label)
        editor_class = dialect.schema_editor_class
        rewrites_table = editor_class.column_type_changes_class.rewrites_table_on_alter(
            old_model, new_model, self.name, dialect
        )
        loses_data = editor_class.column_type_changes_class.changes_column_type(
            old_model, new_model, self.name, dialect
        )
        reasons = []
        if loses_data:
            reasons.append(
                f"changes the column type of {self.model_name}.{self.name} - a value the new type can't hold is lost"
            )
        if rewrites_table:
            reasons.append(f"rewrites the table of {self.model_name}")
        return OperationEffect(
            operation=self,
            reversible=self.reversible,
            rewrites_table=rewrites_table,
            loses_data=loses_data,
            reason="; ".join(reasons) or None,
        )

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        old_model = self._model(old_state, app_label)
        new_model = self._model(new_state, app_label)
        await state_editor.alter_field(old_model, new_model, self.name, new_state.get_models_with_tables())

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        # Altering back is altering from the later state to the earlier one - the states come swapped.
        await self.database_forward(app_label, old_state, new_state, state_editor)
