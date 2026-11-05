from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.schema.constants import THROUGH_ROWS_COPY_PREVIEW, THROUGH_ROWS_COPY_SQL
from hare.dialects.base.schema.plans.enums import SchemaEffect

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.fields.field import Field
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class ThroughTableCopy:
    """Prepared relation-row copy with execution-only defaults or generated keys."""

    source_sql: str
    target_sql: str
    columns: tuple[str, ...]
    values: tuple[str, ...]
    distinct: bool
    through_model: type[Model] | None
    defaults: tuple[tuple[str, Field[Any]], ...] = ()
    generated_key_column: str | None = None
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.REWRITE_ROWS, SchemaEffect.BACKFILL_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Evaluate each extra default once and ask the client for its generated-key SQL."""
        if editor.collect_sql:
            editor.collected_sql.append(
                THROUGH_ROWS_COPY_PREVIEW.format(source=self.source_sql, target=self.target_sql)
            )
            return
        columns = list(self.columns)
        values = list(self.values)
        if self.through_model is not None:
            for column, field in self.defaults:
                columns.append(column)
                values.append(
                    await editor.column_backfill.added_column_backfill_sql_literal(field, self.through_model)
                )
            if self.generated_key_column is not None:
                value = await editor.client.get_generated_key_sql(self.through_model)
                if value is not None:
                    columns.append(self.generated_key_column)
                    values.append(value)
        await editor.run_sql(self.get_sql(editor, columns, values))

    def get_sql(self, editor: BaseSchemaEditor, columns: list[str], values: list[str]) -> str:
        """Render the prepared copy after all runtime values are available."""
        return THROUGH_ROWS_COPY_SQL.format(
            source=self.source_sql,
            target=self.target_sql,
            columns=", ".join(editor.quote(column) for column in columns),
            values=", ".join(values),
            distinct="DISTINCT " if self.distinct else "",
        )
