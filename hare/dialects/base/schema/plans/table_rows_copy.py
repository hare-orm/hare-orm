from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.schema.constants import TABLE_COPY_DEFAULT_PREVIEW, TABLE_COPY_SQL
from hare.dialects.base.schema.plans.column_copy import ColumnCopy
from hare.dialects.base.schema.plans.enums import SchemaEffect

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class TableRowsCopy:
    """Copy prepared columns whose clock-derived values are unavailable offline.

    Both table names and column expressions have already been compiled. Execution
    samples each required default once; collection describes the operation without
    sampling a clock or emitting misleading executable SQL with placeholder values.
    """

    model: type[Model]
    target_sql: str
    source_sql: str
    columns: tuple[ColumnCopy, ...]
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.REWRITE_ROWS, SchemaEffect.BACKFILL_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Copy with the current values, or describe the runtime step for preview."""
        if editor.collect_sql:
            editor.collected_sql.append(
                TABLE_COPY_DEFAULT_PREVIEW.format(source=self.source_sql, target=self.target_sql)
            )
            return
        await editor.run_sql(self.get_sql(editor))

    def get_sql(self, editor: BaseSchemaEditor) -> str:
        """Render the common copy statement once its default values are available."""
        return TABLE_COPY_SQL.format(
            target=self.target_sql,
            source=self.source_sql,
            columns=", ".join(column.column_sql for column in self.columns),
            values=", ".join(column.get_value_sql(editor, self.model) for column in self.columns),
        )
