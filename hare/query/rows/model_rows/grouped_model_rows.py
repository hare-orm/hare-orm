from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from hare.dialects.base.constants import CHUNK_SIZE
from hare.models.instances.dirty_fields import DirtyFields
from hare.query.rows.model_rows.model_rows import ModelRows

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.results.statement_result import StatementResult
    from hare.query.rows.model_rows.model_columns import ModelColumns


class GroupedModelRows(ModelRows):
    """Read one projected owner key and related model rows as detached ``(owner, obj)`` pairs.

    The common native layout decodes every input row with its ordinary field readers.
    Construction is shared by decoded primary key, including across chunk boundaries;
    grouping does not retain driver rows or install temporary attributes on the instances.
    """

    def get_result_layout(
        self, columns: list[ModelColumns], annotations: tuple[tuple[str, int | str, Any], ...]
    ) -> Any:
        ((_name, owner_key, owner_reader),) = annotations
        return self.native_rows.GroupedModelLayout(
            columns[0], self.model._meta.primary_key_attribute_names, owner_key, owner_reader
        )

    async def read_all(self, connection: DatabaseClient, result: StatementResult) -> list[Any]:
        layout = self.prepared_result or self.get_result_layout(
            self.get_columns(connection, result), self.get_annotation_layout(connection, result.column_names)
        )
        shared: dict[Any, Any] = {}
        entries: list[Any] = []
        for start in range(0, len(result.rows), CHUNK_SIZE):
            stop = min(start + CHUNK_SIZE, len(result.rows))
            entries.extend(
                layout.read_batch(
                    result.rows,
                    start,
                    stop,
                    connection.connection_alias,
                    DirtyFields.snapshot_dirty_fields,
                    shared,
                )
            )
            if stop - start == CHUNK_SIZE:
                await asyncio.sleep(0)
        return entries
