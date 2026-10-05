from __future__ import annotations

from typing import Any

from hare.ddl.table_options import TableOptions
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError
from hare.models import Model


class TablePartitions(SchemaEditorPart):
    """A partitioned table's options and partitions: the partitions created with the table, added and
    removed."""

    __slots__ = ()

    async def alter_table_options(
        self, model: type[Model], old_options: TableOptions | None, new_options: TableOptions | None
    ) -> None:
        """Applies a change of the table's ``Meta.table_options`` for this dialect - by rebuilding
        the table with the new options, which a dialect whose ALTER TABLE can change them in place
        overrides.

        Args:
            model: The model rendered with its new options.
            old_options: The dialect's previous options, None for none.
            new_options: The dialect's new options, None for none.
        """
        await self.editor.table_rebuild.remake_table(model)

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
