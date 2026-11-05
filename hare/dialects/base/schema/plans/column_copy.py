from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.fields.field import Field
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class ColumnCopy:
    """A prepared target column and its source expression during table replacement.

    Constant defaults are rendered during preparation. Only a clock-derived default
    retains its field for runtime evaluation; ordinary callable defaults are rejected
    by the existing rebuild contract before any DDL. A source plus a default means
    filling NULL values, while a default alone fills a newly introduced column.
    """

    column_sql: str
    source_sql: str | None
    default_sql: str | None = None
    clock_field: Field[Any] | None = None

    def get_value_sql(self, editor: BaseSchemaEditor, model: type[Model]) -> str:
        """Render a value, evaluating the clock only during row-copy execution."""
        default_sql = self.default_sql
        if self.clock_field is not None:
            default_sql = editor.column_backfill.auto_now_backfill_sql_literal(self.clock_field, model)
        if self.source_sql is None:
            return default_sql if default_sql is not None else "NULL"
        if default_sql is None:
            return self.source_sql
        return f"COALESCE({self.source_sql}, {default_sql})"
