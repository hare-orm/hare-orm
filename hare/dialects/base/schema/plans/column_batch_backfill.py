from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.schema.constants import COLUMN_BATCH_BACKFILL_PREVIEW
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.base.client.statement_recovery import StatementRecovery
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.fields.field import Field


@dataclass(frozen=True, slots=True)
class ColumnBatchBackfill:
    """A prepared dialect-specific backfill with one value shared by all batches of one execution.

    A synchronous value factory runs only during execution, once before the first
    statement. Preview may serialize a constant but never invokes the factory.
    Bounded UPDATEs repeat while they report changed rows; statements without a row
    count run once.
    """

    sql: str
    model: type[Model]
    field: Field[Any]
    value: Any
    uses_relation_key: bool
    recovery: StatementRecovery | None = None
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.BACKFILL_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Evaluate once and execute, repeating only while the statement reports changed rows."""
        if editor.collect_sql and callable(self.value):
            editor.collected_sql.append(COLUMN_BATCH_BACKFILL_PREVIEW.format(sql=self.sql))
            return
        raw_value = self.value() if callable(self.value) else self.value
        if self.uses_relation_key and isinstance(raw_value, Model):
            raw_value = raw_value.pk
        value = editor.client.dialect.types.get_db_value(self.field, raw_value, self.model)
        # Positional placeholders each need their parameter, even when both values are equal.
        parameters = [value, value]
        if editor.collect_sql:
            editor.collected_sql.append(
                f"{self.sql}  -- params: {parameters!r}; repeat only while changed rows are reported"
            )
            return
        while await editor.run_parameterized_sql(self.sql, parameters, recovery=self.recovery):
            pass
