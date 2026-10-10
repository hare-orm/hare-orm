from __future__ import annotations

from typing import Any

from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field
from hare.models import Model


class TableComments(SchemaEditorPart):
    """The comments of a table and its columns, written from the model's and the fields' descriptions."""

    __slots__ = ()

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        # Databases have their own way of supporting comments for table level
        raise NotImplementedError()  # pragma: nocoverage

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        # Databases have their own way of supporting comments for column level
        raise NotImplementedError()  # pragma: nocoverage

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
