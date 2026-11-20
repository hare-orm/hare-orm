from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.models.delete_model import DeleteModel
from hare.migrations.operations.models.table_renaming_operation import TableRenamingOperation
from hare.migrations.operations.operation import Operation
from hare.migrations.operations.operation_rewrite import OperationRewrite
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class AlterModelTable(TableRenamingOperation):
    """Change ``Meta.table``. Passing ``None`` takes the table name from the model's name again."""

    def __init__(self, name: str, table: str | None) -> None:
        self.name = name
        self.table = table

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        """The model whose table is renamed."""
        return frozenset({self.get_model_label(self.name, app_label)})

    def get_rewrite(self, other: Operation, app_label: str) -> OperationRewrite | None:
        """A later table name or deletion of the same model takes this one's place."""
        if isinstance(other, (AlterModelTable, DeleteModel)) and self.is_same_model_name(self.name, other.name):
            return OperationRewrite((other,))
        return super().get_rewrite(other, app_label)

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.name,)

    def describe(self) -> str:
        return f"Rename table for {self.name} to {self.table!r}"

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.name)
        model_state.table = ModelState.record_table_option(model_state.options, model_state.name, self.table)
        state.mark_models_changed([(app_label, self.name)])

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._apply_table_rename(app_label, old_state, new_state, state_editor, self.name, self.name)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        # The executor passes the states swapped for a backward run.
        await self._apply_table_rename(app_label, old_state, new_state, state_editor, self.name, self.name)
