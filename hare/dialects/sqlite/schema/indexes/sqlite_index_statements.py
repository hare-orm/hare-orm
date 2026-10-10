from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.indexes.index_statements import IndexStatements
from hare.dialects.sqlite.indexes.own_table_index import OwnTableIndex
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor


class SqliteIndexStatements(IndexStatements):
    """IndexStatements as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_index_create_sql(
        self,
        model: type[Model],
        index: Index,
        index_name: str | None = None,
        indexed_table_sql: str | None = None,
    ) -> str:
        if isinstance(index, OwnTableIndex):
            return "\n".join(index.get_create_sqls(self.editor, model, safe=False))
        return super().get_index_create_sql(model, index, index_name, indexed_table_sql)

    def get_index_drop_sql(self, model: type[Model], index: Index) -> str:
        if isinstance(index, OwnTableIndex):
            index.raise_if_not_droppable(self.editor.client.features, self.editor.client.dialect)
            return "\n".join(
                index.get_drop_sqls(self.editor, model._meta.db_table, model._meta.get_column_names(index.fields))
            )
        return super().get_index_drop_sql(model, index)

    async def drop_indexes_reading_columns_to_rename(self, old_model: type[Model], new_model: type[Model]) -> None:
        """Drops an index kept in a table of its own over a column about to be renamed - it reads
        the column by name, and is named after it."""
        for old_index, _new_index in self.get_indexes_reading_renamed_columns(old_model, new_model):
            await self.editor.drop_own_table_indexes(old_model, (old_index,))

    async def recreate_indexes_reading_renamed_columns(self, old_model: type[Model], new_model: type[Model]) -> None:
        """Creates an index kept in a table of its own over a renamed column again."""
        for _old_index, new_index in self.get_indexes_reading_renamed_columns(old_model, new_model):
            await self.editor.add_index(new_model, new_index)

    def get_indexes_reading_renamed_columns(
        self, old_model: type[Model], new_model: type[Model]
    ) -> list[tuple[OwnTableIndex, OwnTableIndex]]:
        """The indexes kept in tables of their own over a column a rename changes - each before and
        after the rename.

        Args:
            old_model: The model before the rename.
            new_model: The model after it.

        Returns:
            (old index, new index) pairs.
        """
        return [
            (old_index, new_index)
            for old_index, new_index in zip(
                self.editor.get_own_table_indexes(old_model),
                self.editor.get_own_table_indexes(new_model),
                strict=False,
            )
            if old_model._meta.get_column_names(old_index.fields) != new_model._meta.get_column_names(new_index.fields)
        ]
