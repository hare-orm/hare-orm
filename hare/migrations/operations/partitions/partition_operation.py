from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class PartitionOperation(HareOperation):
    """Adds or removes one partition of a model's table - a partition of the model's
    ``Meta.table_options`` entry accepting it (``TableOptions.accepts_partition``). On a connection whose
    dialect takes another entry only the state changes.

    Args:
        model_name: The model.
        partition: The partition.
    """

    def __init__(self, model_name: str, partition: Any) -> None:
        self.model_name = model_name
        self.partition = partition

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        """The partitioned model."""
        return frozenset({self.get_model_label(self.model_name, app_label)})

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.model_name,)

    def _set_partition_in_state(self, app_label: str, state: State, *, present: bool) -> None:
        """Adds the partition to, or removes it from, the model's table options in the state.

        Args:
            app_label: The migration's app label.
            state: The state, changed in place.
            present: Whether the partition is there afterwards.

        Raises:
            IncompatibleStateError: The model has no table options accepting the partition, the
                partition to add is already there, or the one to remove isn't.
        """
        model_state = self.get_model_state(state, app_label, self.model_name)
        table_options = tuple(model_state.options.get(ModelOption.TABLE_OPTIONS, ()))
        partition_name = self.partition.name
        if not any(entry.accepts_partition(self.partition) for entry in table_options):
            raise IncompatibleStateError(
                f"{self.model_name} has no Meta.table_options holding the partition {partition_name!r}"
            )
        changed_table_options = []
        for entry in table_options:
            if entry.accepts_partition(self.partition):
                partitions = entry.get_partitions()
                if present == (partition_name in partitions):
                    problem = "already has a" if present else "has no"
                    raise IncompatibleStateError(f"{self.model_name} {problem} partition {partition_name!r}")
                if present:
                    partitions[partition_name] = self.partition
                else:
                    del partitions[partition_name]
                entry = entry.with_partitions(partitions)
            changed_table_options.append(entry)
        model_state.options[ModelOption.TABLE_OPTIONS] = tuple(changed_table_options)
        state.mark_models_changed([(app_label, self.model_name)])

    async def _apply_to_table(
        self, app_label: str, table_state: State, state_editor: BaseSchemaEditor | None, *, add: bool
    ) -> None:
        """Creates or drops the partition's table on a connection whose dialect takes the entry
        holding the partition.

        Args:
            app_label: The migration's app label.
            table_state: The state whose model names the table - with the partition, for adding.
            state_editor: The schema editor, or None for a state-only run.
            add: Whether the partition is added, else removed.
        """
        if not state_editor:
            return
        model = table_state.apps.get_model(f"{app_label}.{self.model_name}")
        if model._meta.managed is False:
            return
        table_options = state_editor.client.dialect.get_declared_table_options(model._meta.table_options)
        if table_options is None or not table_options.accepts_partition(self.partition):
            return
        if add:
            await state_editor.table_partitions.add_partition(model, self.partition)
        else:
            await state_editor.table_partitions.remove_partition(model, self.partition)
