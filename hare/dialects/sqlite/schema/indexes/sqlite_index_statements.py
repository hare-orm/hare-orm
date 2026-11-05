from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.indexes.index_statements import IndexStatements
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.sqlite.indexes.separate_table_index import SeparateTableIndex
from hare.models import Model

if TYPE_CHECKING:
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
        safe: bool = False,
        *,
        template: str | None = None,
    ) -> str:
        if isinstance(index, SeparateTableIndex):
            return "\n".join(index.get_create_sqls(self.editor, model, safe=safe))
        return super().get_index_create_sql(model, index, index_name, indexed_table_sql, safe, template=template)

    def get_index_drop_sql(self, model: type[Model], index: Index) -> str:
        if isinstance(index, SeparateTableIndex):
            index.raise_if_not_droppable(self.editor.client.features, self.editor.client.dialect)
            return "\n".join(
                index.get_drop_sqls(
                    self.editor,
                    model._meta.db_table,
                    model._meta.get_column_names(index.fields),
                    model._meta.schema,
                )
            )
        return super().get_index_drop_sql(model, index)

    def get_indexes_before_column_rename_plan(
        self, old_model: type[Model], new_model: type[Model]
    ) -> SchemaChangePlan:
        """Prepares dropping an index kept in a table of its own over a column about to be renamed - it reads
        the column by name, and is named after it."""
        return self.editor.get_separate_index_drop_plan(
            old_model,
            tuple(old_index for old_index, _ in self.get_indexes_reading_renamed_columns(old_model, new_model)),
        )

    def get_indexes_after_column_rename_plan(self, old_model: type[Model], new_model: type[Model]) -> SchemaChangePlan:
        """Prepares recreating separate-table indexes after the column rename."""
        return SchemaChangePlan(
            tuple(
                step
                for _, new_index in self.get_indexes_reading_renamed_columns(old_model, new_model)
                for step in self.editor.get_add_index_plan(new_model, new_index).steps
            )
        )

    def get_indexes_reading_renamed_columns(
        self, old_model: type[Model], new_model: type[Model]
    ) -> list[tuple[SeparateTableIndex, SeparateTableIndex]]:
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
                self.editor.get_separate_table_indexes(old_model),
                self.editor.get_separate_table_indexes(new_model),
                strict=False,
            )
            if old_model._meta.get_column_names(old_index.fields) != new_model._meta.get_column_names(new_index.fields)
        ]
