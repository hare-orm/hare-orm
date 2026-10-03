from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.partitions.partition_operation import PartitionOperation
from hare.migrations.state.project.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class AddPartition(PartitionOperation):
    """Adds a partition to a model's partitioned table: an empty table attached to it for the rows
    of the partition's bound. Unapplying it removes the partition with the rows it got meanwhile.

    Args:
        model_name: The model.
        partition: The partition - a ``ListPartition``, ``RangePartition`` or ``DefaultPartition``
            on PostgreSQL.
    """

    def describe(self) -> str:
        return f"Add partition {self.partition.name} to {self.model_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        self._set_partition_in_state(app_label, state, present=True)

    async def database_forward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        await self._apply_to_table(app_label, new_state, state_editor, add=True)

    async def database_backward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        await self._apply_to_table(app_label, old_state, state_editor, add=False)
