from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding

from typing import Any

from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError


class TablePartitions(SchemaEditorPart):
    """A partitioned table's options and partitions: the partitions created with the table, added and
    removed."""

    __slots__ = ()

    async def alter_table_options(self, old_binding: ModelBinding, new_binding: ModelBinding) -> None:
        """Execute a prepared transition between both physical table declarations."""
        await self.get_alter_table_options_plan(old_binding, new_binding).execute(self.editor)

    def get_alter_table_options_plan(self, old_binding: ModelBinding, new_binding: ModelBinding) -> SchemaChangePlan:
        """Rebuild by default; dialects may compile supported in-place changes."""
        return self.editor.table_rebuild.get_rebuild_plan(old_binding, new_binding)

    def get_partition_create_sqls(self, binding: ModelBinding, safe: bool) -> list[str]:
        """The statements creating the partitions of a model's table, run right after its
        ``CREATE TABLE`` - none on a dialect without partitioned tables.

        Args:
            binding: The model.
            safe: Whether each partition is created only when it doesn't exist yet.

        Returns:
            The statements.
        """
        return []

    def get_partition_locations(self, binding: ModelBinding) -> dict[str, tuple[str | None, str]]:
        """The physical (schema, table) of each named partition."""
        return {}

    async def rename_partition(self, binding: ModelBinding, partition: Any, replacement: Any) -> None:
        """Change the physical identity of a partition without changing its bound."""
        raise UnSupportedError(f"The {self.editor.client.dialect.name} dialect can't rename a partition")

    async def add_partition(self, binding: ModelBinding, partition: Any) -> None:
        """Adds a partition to a model's table (``TableOptions.get_partitions()``).

        Args:
            binding: The model rendered with the partition.
            partition: The partition.

        Raises:
            UnSupportedError: The dialect has no partitions to add one at a time.
        """
        raise UnSupportedError(f"The {self.editor.client.dialect.name} dialect can't add a partition to a table")

    async def remove_partition(self, binding: ModelBinding, partition: Any) -> None:
        """Removes a partition of a model's table with its rows.

        Args:
            binding: The model.
            partition: The partition.

        Raises:
            UnSupportedError: The dialect has no partitions to remove one at a time.
        """
        raise UnSupportedError(f"The {self.editor.client.dialect.name} dialect can't remove a partition of a table")
