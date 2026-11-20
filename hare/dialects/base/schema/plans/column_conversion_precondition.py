from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.schema.constants import COLUMN_CONVERSION_CHECK_PREVIEW
from hare.dialects.base.schema.plans.enums import SchemaEffect

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.fields.field import Field


@dataclass(frozen=True, slots=True)
class ColumnConversionPrecondition:
    """Check stored values before a prepared type change can modify the schema."""

    table_sql: str
    column_sql: str
    old_field: Field[Any]
    new_field: Field[Any]
    check_narrowing: bool
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.CHECK_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Apply the dialect's conversion and lossy-narrowing checks without DDL."""
        if editor.collect_sql:
            editor.collected_sql.append(
                COLUMN_CONVERSION_CHECK_PREVIEW.format(table=self.table_sql, column=self.column_sql)
            )
            return
        await editor.column_conversion_check.check_alter_field_conversion(
            self.table_sql, self.old_field, self.new_field, self.column_sql
        )
        if self.check_narrowing:
            await editor.column_narrowing_check.check_alter_field_narrowing_data_loss(
                self.table_sql, self.old_field, self.new_field, self.column_sql
            )
