from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.schema.constants import COLUMN_DEFAULT_BACKFILL_PREVIEW
from hare.dialects.base.schema.plans.enums import SchemaEffect

if TYPE_CHECKING:
    from hare.dialects.base.client.statement_recovery import StatementRecovery
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.fields.field import Field
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class ColumnDefaultBackfill:
    """A callable or clock-derived default, evaluated only when its step executes.

    One evaluation supplies all existing NULL rows, as adding a required column
    promises. Preview describes the action without calling a function, awaiting a
    default or sampling the clock. Constant defaults use a prepared SQL step.
    """

    model: type[Model]
    field: Field[Any]
    table_sql: str
    column_sql: str
    recovery: StatementRecovery | None = None
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.BACKFILL_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Compute the default once, then execute the dialect's prepared backfill form."""
        if editor.collect_sql:
            editor.collected_sql.append(
                COLUMN_DEFAULT_BACKFILL_PREVIEW.format(table=self.table_sql, column=self.column_sql)
            )
            return
        value = await editor.column_backfill.added_column_backfill_sql_literal(self.field, self.model)
        await editor.run_sql(
            editor.column_backfill.get_fill_nulls_sql(self.table_sql, self.column_sql, value), recovery=self.recovery
        )
