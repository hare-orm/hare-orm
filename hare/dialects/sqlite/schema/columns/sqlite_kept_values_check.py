from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.sqlite.schema.constants import SQLITE_KEPT_VALUES_CHECK_PREVIEW

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.fields.field import Field


@dataclass(frozen=True, slots=True)
class SqliteKeptValuesCheck:
    """Validate stored values when a changed field keeps its physical column."""

    table_sql: str
    column_sql: str
    field: Field[Any]
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.CHECK_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Keep Python-field validation separate from SQL column replacement."""
        if editor.collect_sql:
            editor.collected_sql.append(
                SQLITE_KEPT_VALUES_CHECK_PREVIEW.format(table=self.table_sql, column=self.column_sql)
            )
            return
        await editor.column_conversion_check.check_kept_values(self.table_sql, self.field, self.column_sql)  # type: ignore[attr-defined]
