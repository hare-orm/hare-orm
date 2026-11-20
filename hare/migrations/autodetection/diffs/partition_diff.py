from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.partition_changes import PartitionChanges
from hare.migrations.operations import AddPartition, HareOperation, RemovePartition, RenamePartition
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.ddl.table_options import TableOptions
    from hare.migrations.state.model_state import ModelState


class PartitionDiff:
    """The partition operations of a model: where a dialect's ``Meta.table_options`` entry reaches
    its new partitions by adding, removing or renaming them one at a time
    (``TableOptions.can_change_partitions_to()``), each is written as an ``AddPartition``, ``RemovePartition``
    or ``RenamePartition``. ``AlterModelOptions`` carries the remaining changes. A partition whose definition
    changed is removed and added again.

    Args:
        old_state: The model's state before.
        new_state: The model's state after.
    """

    def __init__(self, old_state: ModelState, new_state: ModelState) -> None:
        self.old_state = old_state
        self.new_state = new_state
        #: A warning per partition removed - its rows go with it.
        self.data_loss_warnings: list[str] = []

    def _get_changed_entries(self) -> list[tuple[TableOptions, TableOptions]]:
        """The table options entries, before and after, whose partitions are added or removed one
        at a time.

        Returns:
            The (old, new) pairs.
        """
        # An entry before and after is the one of the same dialect's options class.
        old_entries = {type(entry): entry for entry in self.old_state.options.get(ModelOption.TABLE_OPTIONS, ())}
        changed_entries = []
        for new_entry in self.new_state.options.get(ModelOption.TABLE_OPTIONS, ()):
            old_entry = old_entries.get(type(new_entry))
            if (
                old_entry is not None
                and old_entry.can_change_partitions_to(new_entry)
                and old_entry.get_partitions() != new_entry.get_partitions()
            ):
                changed_entries.append((old_entry, new_entry))
        return changed_entries

    def get_operations(self) -> list[HareOperation]:
        """The operations removing and adding the partitions that changed.

        Returns:
            Removal operations, followed by renames and additions.
        """
        operations: list[HareOperation] = []
        model_name = self.new_state.name
        for old_entry, new_entry in self._get_changed_entries():
            old_partitions, new_partitions = old_entry.get_partitions(), new_entry.get_partitions()
            changes = self.get_partition_changes(model_name, old_partitions, new_partitions)
            operations.extend(changes)
            for operation in changes:
                if isinstance(operation, RemovePartition):
                    name = operation.partition.name
                    self.data_loss_warnings.append(
                        f"{model_name}: the partition {name!r} is removed with its rows"
                        + (" and added again empty - its definition changed" if name in new_partitions else "")
                    )
        return operations

    @staticmethod
    def get_partition_changes(model_name: str, old: dict[str, Any], new: dict[str, Any]) -> list[HareOperation]:
        """Use the same partition operations for model changes and catalog drift."""
        changes = PartitionChanges.compare(old, new)
        return [
            *(RemovePartition(model_name, partition) for partition in changes.removed),
            *(RenamePartition(model_name, partition, replacement) for partition, replacement in changes.renamed),
            *(AddPartition(model_name, partition) for partition in changes.added),
        ]

    def get_options_before_partition_operations(self, new_options: dict[str, Any]) -> dict[str, Any]:
        """The model's new options as ``AlterModelOptions`` sets them when the partition operations
        follow it: every entry they change still holds its old partitions.

        Args:
            new_options: The model's new options.

        Returns:
            The options - ``new_options`` itself when no partition changes one at a time.
        """
        changed_entries = self._get_changed_entries()
        if not changed_entries:
            return new_options
        old_partitions_by_options_class = {
            type(old_entry): old_entry.get_partitions() for old_entry, _new in changed_entries
        }
        return {
            **new_options,
            ModelOption.TABLE_OPTIONS: tuple(
                entry.with_partitions(old_partitions_by_options_class[type(entry)])
                if type(entry) in old_partitions_by_options_class
                else entry
                for entry in new_options[ModelOption.TABLE_OPTIONS]
            ),
        }
