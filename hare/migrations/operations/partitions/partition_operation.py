from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.state.state import State
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models.model_binding import ModelBinding


class PartitionOperation(ModelBoundOperation):
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

    def _change_partition_in_state(self, app_label: str, state: State, *, remove: Any, add: Any) -> None:
        """Apply one partition change to the model's table options.

        Args:
            app_label: The migration's app label.
            state: The state, changed in place.
            remove: The partition being removed, or None.
            add: The partition being added, or None.

        Raises:
            IncompatibleStateError: The model has no table options accepting the partition or its
                partitioning cannot represent the resulting set of partitions.
        """
        model_state = self.get_model_state(state, app_label, self.model_name)
        table_options = tuple(model_state.options.get(ModelOption.TABLE_OPTIONS, ()))
        found = False
        changed_table_options = []
        for entry in table_options:
            if entry.accepts_partition(self.partition):
                found = True
                partitions = entry.get_partitions()
                if remove is not None:
                    recorded = partitions.get(remove.name)
                    if recorded is None:
                        raise IncompatibleStateError(f"{self.model_name} has no partition {remove.name!r}")
                    if recorded != remove:
                        raise IncompatibleStateError(f"{self.model_name} has a different partition {remove.name!r}")
                    del partitions[remove.name]
                if add is not None:
                    if add.name in partitions:
                        raise IncompatibleStateError(f"{self.model_name} already has a partition {add.name!r}")
                    partitions[add.name] = add
                entry = entry.with_partitions(partitions)
                if entry.get_partitions() != partitions:
                    raise IncompatibleStateError(
                        f"{self.model_name} partitioning cannot represent the change to {self.partition.name!r}"
                    )
            changed_table_options.append(entry)
        if not found:
            raise IncompatibleStateError(
                f"{self.model_name} has no Meta.table_options holding the partition {self.partition.name!r}"
            )
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
        if state_editor is None:
            return
        binding = self._get_partition_binding(app_label, table_state, state_editor, self.partition)
        if binding is None:
            return
        if add:
            await state_editor.table_partitions.add_partition(binding, self.partition)
        else:
            await state_editor.table_partitions.remove_partition(binding, self.partition)

    def _get_partition_binding(
        self, app_label: str, state: State, editor: BaseSchemaEditor, partition: Any
    ) -> ModelBinding | None:
        binding = self.get_model_binding(state, app_label)
        if binding.managed is False:
            return None
        options = editor.client.dialect.get_declared_table_options(binding.table_options)
        return binding if options is not None and options.accepts_partition(partition) else None
