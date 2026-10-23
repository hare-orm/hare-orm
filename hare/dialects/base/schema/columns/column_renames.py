from __future__ import annotations

from typing import Any

from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models import Model


class ColumnRenames(SchemaEditorPart):
    """The columns a rename changes - renamed in place by default (``ALTER TABLE ... RENAME
    COLUMN``)."""

    __slots__ = ()

    @staticmethod
    def get_column_names(field: Field[Any]) -> tuple[str, ...]:
        """The columns a field of a rendered model occupies.

        Args:
            field: The field.

        Returns:
            The column names, one per key column of a relation.
        """
        if isinstance(field, ForeignKeyFieldInstance) and field.db_column_names:
            return field.db_column_names
        return (field.source_field or field.model_field_name,)

    async def rename_model_columns(
        self,
        old_model: type[Model],
        new_model: type[Model],
        field_pairs: list[tuple[Field[Any], Field[Any]]],
    ) -> bool:
        """Renames the columns of a model's fields whose names changed.

        Args:
            old_model: The model before the rename.
            new_model: The model after it.
            field_pairs: The (old, new) fields whose columns change.

        Returns:
            Whether the table was remade - its indexes created under the names the new model gives
            them; False for columns renamed in place.
        """
        for old_field, new_field in field_pairs:
            await self.rename_table_columns(
                new_model._meta.db_table,
                new_model._meta.schema,
                self.get_column_names(old_field),
                self.get_column_names(new_field),
            )
        return False

    async def rename_table_columns(
        self, table_name: str, schema: str | None, old_columns: tuple[str, ...], new_columns: tuple[str, ...]
    ) -> None:
        """Renames each column of a table whose name changed.

        Args:
            table_name: The table.
            schema: Its schema, None for the default one.
            old_columns: The columns before.
            new_columns: The same columns after, in the same order.
        """
        editor = self.editor
        qualified_table = editor.qualify_table_name(table_name, schema)
        for old_column, new_column in zip(old_columns, new_columns, strict=True):
            if old_column == new_column:
                continue
            await editor.run_sql(
                editor.RENAME_FIELD_TEMPLATE.format(
                    table=qualified_table, old_column=editor.quote(old_column), new_column=editor.quote(new_column)
                )
            )
