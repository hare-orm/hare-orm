from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.constraints.constraint_names import ConstraintNames

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor


class SqliteConstraintNames(ConstraintNames):
    """ConstraintNames as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    async def get_unique_constraint_names_from_db(
        self, table_name: str, column_names: list[str], schema: str | None = None
    ) -> list[str]:
        """Use PRAGMA index_list + PRAGMA index_info to find unique index names."""
        _, indexes = await self.editor.client.execute(f'PRAGMA index_list("{table_name}")')
        result: list[str] = []
        for index_row in indexes:
            if not index_row["unique"]:
                continue
            index_name = index_row["name"]
            _, columns = await self.editor.client.execute(f'PRAGMA index_info("{index_name}")')
            index_column_names = [column["name"] for column in columns]
            if index_column_names == column_names:
                result.append(index_name)
        return result
