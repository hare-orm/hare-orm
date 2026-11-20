from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.partitions.partition_operation import PartitionOperation
from hare.migrations.reports.operation_effect import OperationEffect
from hare.migrations.state.state import State

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class RemovePartition(PartitionOperation):
    """Removes a partition of a model's partitioned table: the partition is detached and its table
    dropped - WITH ITS ROWS. Unapplying it adds the partition back, empty.

    Args:
        model_name: The model.
        partition: The partition as it was declared - a ``ListPartition``, ``RangePartition`` or
            ``DefaultPartition`` of the dialect's partitioning.
    """

    def describe(self) -> str:
        return f"Remove partition {self.partition.name} of {self.model_name} with its rows"

    def get_effect(
        self, app_label: str, old_state: State, new_state: State, schema_editor: BaseSchemaEditor
    ) -> OperationEffect:
        return OperationEffect(operation=self, reversible=self.reversible, loses_data=True, reason=self.describe())

    def state_forward(self, app_label: str, state: State) -> None:
        self._change_partition_in_state(app_label, state, remove=self.partition, add=None)

    async def database_forward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        await self._apply_to_table(app_label, old_state, state_editor, add=False)

    async def database_backward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        await self._apply_to_table(app_label, new_state, state_editor, add=True)
