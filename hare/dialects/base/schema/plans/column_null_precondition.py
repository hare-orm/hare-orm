from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.schema.constants import COLUMN_NULL_CHECK_PREVIEW
from hare.dialects.base.schema.plans.enums import SchemaEffect

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class ColumnNullPrecondition:
    """Require no stored NULL values when a NOT NULL change has no backfill."""

    table_sql: str
    column_sql: str
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.CHECK_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Read existing rows only during execution, with an explicit offline preview."""
        if editor.collect_sql:
            editor.collected_sql.append(COLUMN_NULL_CHECK_PREVIEW.format(table=self.table_sql, column=self.column_sql))
            return
        await editor.column_narrowing_check.check_not_null_change(self.table_sql, self.column_sql)
