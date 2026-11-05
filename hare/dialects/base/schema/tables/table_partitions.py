from __future__ import annotations

from typing import Any

from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError
from hare.models import Model


class TablePartitions(SchemaEditorPart):
    """A partitioned table's options and partitions: the partitions created with the table, added and
    removed."""

    __slots__ = ()

    async def alter_table_options(self, old_model: type[Model], new_model: type[Model]) -> None:
        """Execute a prepared transition between both physical table declarations."""
        await self.get_alter_table_options_plan(old_model, new_model).execute(self.editor)

    def get_alter_table_options_plan(self, old_model: type[Model], new_model: type[Model]) -> SchemaChangePlan:
        """Rebuild by default; dialects may compile supported in-place changes."""
        return self.editor.table_rebuild.get_rebuild_plan(old_model, new_model)

    def get_partition_create_sqls(self, model: type[Model], safe: bool) -> list[str]:
        """The statements creating the partitions of a model's table, run right after its
        ``CREATE TABLE`` - none on a dialect without partitioned tables.

        Args:
            model: The model.
            safe: Whether each partition is created only when it doesn't exist yet.

        Returns:
            The statements.
        """
        return []

    def get_partition_table_names(self, model: type[Model]) -> list[str]:
        """The tables the partitions of a model's table are - none on a dialect whose partitions
        are no tables of their own, and for a table without partitions.

        Args:
            model: The model.

        Returns:
            The table names, without their schema.
        """
        return []

    async def add_partition(self, model: type[Model], partition: Any) -> None:
        """Adds a partition to a model's table (``TableOptions.get_partitions()``).

        Args:
            model: The model rendered with the partition.
            partition: The partition.

        Raises:
            UnSupportedError: The dialect has no partitions to add one at a time.
        """
        raise UnSupportedError(f"The {self.editor.client.dialect.name} dialect can't add a partition to a table")

    async def remove_partition(self, model: type[Model], partition: Any) -> None:
        """Removes a partition of a model's table with its rows.

        Args:
            model: The model.
            partition: The partition.

        Raises:
            UnSupportedError: The dialect has no partitions to remove one at a time.
        """
        raise UnSupportedError(f"The {self.editor.client.dialect.name} dialect can't remove a partition of a table")
