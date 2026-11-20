from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field


class ColumnConversionCheck(SchemaEditorPart):
    """The check of the values a column's type change converts, made before the column is altered -
    nothing by default: a database converting the values in one statement refuses the change itself
    and keeps the column as it was."""

    __slots__ = ()

    async def check_alter_field_conversion(
        self, qualified_table: str, old_field: Field[Any], new_field: Field[Any], quoted_column: str
    ) -> None:
        """Refuses a column change when a stored value doesn't convert to the new field's type.

        Args:
            qualified_table: The already schema-qualified, quoted table name.
            old_field: The field's previous definition.
            new_field: Its new definition.
            quoted_column: The already-quoted column.
        """

    @staticmethod
    def get_shown_conversion_value(old_field: Field[Any], new_field: Field[Any], value: Any) -> str:
        """A conversion diagnostic protects values sensitive in either column definition."""
        field = old_field if old_field.sensitive else new_field
        return field.get_value_for_message(value)

    async def get_unread_value(self, qualified_table: str, quoted_column: str, new_field: Field[Any]) -> Any | None:
        """A stored value the new field's reader doesn't read.

        Args:
            qualified_table: The already schema-qualified, quoted table name.
            quoted_column: The already-quoted column.
            new_field: The field's new definition.

        Returns:
            One raw stored value, None when every non-NULL value is read. The caller formats it
            with both the old and the new field's diagnostic policy.
        """
        read_value = self.editor.client.dialect.types.get_python_reader(new_field)
        for value in await self.iter_distinct_stored_values(qualified_table, quoted_column):
            try:
                read_value(value)
            except (ValueError, TypeError, ArithmeticError):
                # The reader refuses a value it doesn't read.
                return value
        return None

    async def iter_distinct_stored_values(self, qualified_table: str, quoted_column: str) -> Iterator[Any]:
        """Read distinct non-NULL values from the already quoted table and column."""
        _, rows = await self.editor.client.execute(
            f"SELECT DISTINCT {quoted_column} AS value FROM {qualified_table} "  # nosec B608
            f"WHERE {quoted_column} IS NOT NULL"
        )
        return (row["value"] for row in rows)
