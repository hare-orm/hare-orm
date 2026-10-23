from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field
from hare.models import Model


class TableComments(SchemaEditorPart):
    """The comments of a table and its columns, written from the model's and the fields' descriptions."""

    __slots__ = ()

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        # Databases have their own way of supporting comments for table level
        raise NotImplementedError()

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        # Databases have their own way of supporting comments for column level
        raise NotImplementedError()

    def get_table_comment_statements(self, table: str, comment: str) -> list[str]:
        """The statements setting a table's comment, for a database that sets it apart from
        ``CREATE TABLE`` - none by default.

        Args:
            table: The qualified table.
            comment: The comment.

        Returns:
            The statements.
        """
        return []

    def get_column_comment_statements(self, table: str, column: str, comment: str) -> list[str]:
        """The statements setting a column's comment, for a database that sets it apart from the
        column's definition - none by default.

        Args:
            table: The qualified table.
            column: The column.
            comment: The comment.

        Returns:
            The statements.
        """
        return []

    def get_model_comment_statements(
        self, model: type[Model], column_field_names: Iterable[str] | None = None
    ) -> list[str]:
        """The statements setting the comments of a model's table and columns apart from their
        definitions - the table's and every column's, or only the given columns'.

        Args:
            model: The model.
            column_field_names: The fields of the columns; None for the whole table.

        Returns:
            The statements.
        """
        meta = model._meta
        qualified_table = self.editor.qualify_table_name(meta.db_table, meta.schema)
        statements: list[str] = []
        if column_field_names is None:
            if meta.table_description:
                statements.extend(self.get_table_comment_statements(qualified_table, meta.table_description))
            column_field_names = meta.fields_db_projection
        for field_name in column_field_names:
            description = self.editor.column_definitions.get_column_description(model, field_name)
            if description:
                statements.extend(
                    self.get_column_comment_statements(
                        qualified_table, meta.fields_db_projection[field_name], description
                    )
                )
        return statements

    async def alter_column_comment(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> None:
        """Alter column comment. Override in backends that support column comments."""
        pass

    async def alter_table_comment(self, model: type[Model]) -> None:
        """Sets the table comment to the model's ``table_description`` (removes it when empty).
        No-op by default - overridden by backends with separately stored table comments.

        Args:
            model: The model, rendered with its new description.
        """
        return
