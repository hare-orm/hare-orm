from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.migrations.operations.partitions.partition_operation import PartitionOperation

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class RenamePartition(PartitionOperation):
    """Change a partition's physical address while keeping its bounds and rows."""

    def __init__(self, model_name: str, partition: Any, replacement: Any) -> None:
        super().__init__(model_name, partition)
        if not partition.has_same_bound(replacement):
            raise ConfigurationError("RenamePartition requires the same bound before and after.")
        self.replacement = replacement

    def describe(self) -> str:
        return f"Rename partition {self.partition.name} of {self.model_name} to {self.replacement.name}"

    def state_forward(self, app_label: str, state: State) -> None:
        self._change_partition_in_state(app_label, state, remove=self.partition, add=self.replacement)

    async def database_forward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        await self._apply_rename(app_label, old_state, state_editor, self.partition, self.replacement)

    async def database_backward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        await self._apply_rename(app_label, old_state, state_editor, self.replacement, self.partition)

    async def _apply_rename(
        self, app_label: str, state: State, editor: BaseSchemaEditor | None, partition: Any, replacement: Any
    ) -> None:
        if editor is None:
            return
        binding = self._get_partition_binding(app_label, state, editor, partition)
        if binding is not None:
            await editor.table_partitions.rename_partition(binding, partition, replacement)
