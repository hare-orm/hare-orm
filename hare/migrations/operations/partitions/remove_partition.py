from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.partitions.partition_operation import PartitionOperation
from hare.migrations.state.project.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class RemovePartition(PartitionOperation):
    """Removes a partition of a model's partitioned table: the partition is detached and its table
    dropped - WITH ITS ROWS. Unapplying it adds the partition back, empty.

    Args:
        model_name: The model.
        partition: The partition as it was declared - a ``ListPartition``, ``RangePartition`` or
            ``DefaultPartition`` on PostgreSQL.
    """

    def describe(self) -> str:
        return f"Remove partition {self.partition.name} of {self.model_name} with its rows"

    def state_forward(self, app_label: str, state: State) -> None:
        self._set_partition_in_state(app_label, state, present=False)

    async def database_forward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        await self._apply_to_table(app_label, old_state, state_editor, add=False)

    async def database_backward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        await self._apply_to_table(app_label, new_state, state_editor, add=True)
