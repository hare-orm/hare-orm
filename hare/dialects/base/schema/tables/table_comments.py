from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field
from hare.models.model_binding import ModelBinding


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
        self, binding: ModelBinding, column_field_names: Iterable[str] | None = None
    ) -> list[str]:
        """The statements setting the comments of a model's table and columns apart from their
        definitions - the table's and every column's, or only the given columns'.

        Args:
            binding: The model.
            column_field_names: The fields of the columns; None for the whole table.

        Returns:
            The statements.
        """
        qualified_table = self.editor.qualify_table_name(binding.db_table, binding.schema)
        statements: list[str] = []
        if column_field_names is None:
            if binding.table_description:
                statements.extend(self.get_table_comment_statements(qualified_table, binding.table_description))
            column_field_names = binding.fields_db_projection
        for field_name in column_field_names:
            description = self.editor.column_definitions.get_column_description(binding, field_name)
            if description:
                statements.extend(
                    self.get_column_comment_statements(
                        qualified_table, binding.fields_db_projection[field_name], description
                    )
                )
        return statements

    def get_column_comment_change_sqls(self, binding: ModelBinding, new_field: Field[Any]) -> list[str]:
        """Prepare a separate comment change where the dialect supports one."""
        return []

    async def alter_table_comment(self, old_binding: ModelBinding, new_binding: ModelBinding) -> None:
        """Execute the prepared table comment transition."""
        await self.get_table_comment_plan(old_binding, new_binding).execute(self.editor)

    def get_table_comment_plan(self, old_binding: ModelBinding, new_binding: ModelBinding) -> SchemaChangePlan:
        """Prepare a separately stored comment change; empty for a dialect without one."""
        return SchemaChangePlan(())
