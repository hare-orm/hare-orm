from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.models.table_renaming_operation import TableRenamingOperation
from hare.migrations.state.project.state import State
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class AlterModelTable(TableRenamingOperation):
    """Changes only the table name (``Meta.table``) - the model's name stays. ``RenameModel`` changes
    the name.
    """

    def __init__(self, name: str, table: str) -> None:
        self.name = name
        self.table = table

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.name,)

    def describe(self) -> str:
        return f"Rename table for {self.name} to {self.table!r}"

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.name)
        model_state.table = self.table
        model_state.options[ModelOption.TABLE] = self.table
        # A later RenameModel keeps an explicit table and renames one derived from the class name.
        model_state.options[ModelOption.TABLE_IS_EXPLICIT] = self.table != self.name.lower()
        state.reload_model(app_label, self.name)

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
