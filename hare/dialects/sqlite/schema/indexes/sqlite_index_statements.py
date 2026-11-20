from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.indexes.index_statements import IndexStatements
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.sqlite.indexes.separate_table_index import SeparateTableIndex

if TYPE_CHECKING:
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.models.model_binding import ModelBinding


class SqliteIndexStatements(IndexStatements):
    """IndexStatements as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_index_create_sql(
        self,
        binding: ModelBinding,
        index: Index,
        index_name: str | None = None,
        indexed_table_sql: str | None = None,
        safe: bool = False,
        *,
        template: str | None = None,
    ) -> str:
        if isinstance(index, SeparateTableIndex):
            return "\n".join(index.get_create_sqls(self.editor, binding, safe=safe))
        return super().get_index_create_sql(binding, index, index_name, indexed_table_sql, safe, template=template)

    def get_index_drop_sql(self, binding: ModelBinding, index: Index) -> str:
        if isinstance(index, SeparateTableIndex):
            index.raise_if_not_droppable(self.editor.client.features, self.editor.client.dialect)
            return "\n".join(
                index.get_drop_sqls(
                    self.editor,
                    binding.db_table,
                    binding.get_column_names(index.fields),
                    binding.schema,
                )
            )
        return super().get_index_drop_sql(binding, index)

    def get_indexes_before_column_rename_plan(
        self, old_binding: ModelBinding, new_binding: ModelBinding
    ) -> SchemaChangePlan:
        """Prepares dropping an index kept in a table of its own over a column about to be renamed - it reads
        the column by name, and is named after it."""
        return self.editor.get_separate_index_drop_plan(
            old_binding,
            tuple(old_index for old_index, _ in self.get_indexes_reading_renamed_columns(old_binding, new_binding)),
        )

    def get_indexes_after_column_rename_plan(
        self, old_binding: ModelBinding, new_binding: ModelBinding
    ) -> SchemaChangePlan:
        """Prepares recreating separate-table indexes after the column rename."""
        return SchemaChangePlan(
            tuple(
                step
                for _, new_index in self.get_indexes_reading_renamed_columns(old_binding, new_binding)
                for step in self.editor.get_add_index_plan(new_binding, new_index).steps
            )
        )

    def get_indexes_reading_renamed_columns(
        self, old_binding: ModelBinding, new_binding: ModelBinding
    ) -> list[tuple[SeparateTableIndex, SeparateTableIndex]]:
        """The indexes kept in tables of their own over a column a rename changes - each before and
        after the rename.

        Args:
            old_binding: The binding of the model before the rename.
            new_binding: The binding of the model after it.

        Returns:
            (old index, new index) pairs.
        """
        return [
            (old_index, new_index)
            for old_index, new_index in zip(
                self.editor.get_separate_table_indexes(old_binding),
                self.editor.get_separate_table_indexes(new_binding),
                strict=False,
            )
            if old_binding.get_column_names(old_index.fields) != new_binding.get_column_names(new_index.fields)
        ]
