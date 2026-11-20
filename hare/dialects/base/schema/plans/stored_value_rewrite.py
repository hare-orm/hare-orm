from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.schema.constants import STORED_VALUE_REWRITE_BATCH_SIZE, STORED_VALUE_REWRITE_PREVIEW
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_cleanup import SchemaCleanup
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.stored_value_snapshot import StoredValueSnapshot

if TYPE_CHECKING:
    from hare.dialects.base.client.statement_recovery import StatementRecovery
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class StoredValueRewrite:
    """Prepared Python conversion of stored values in primary-key ordered batches.

    A key rewrite stages its original rows in the database before any writes, since
    updates may move keys across batch boundaries. Every client read is bounded.
    Other columns retain bounded reads: offsets count
    processed rows that remain non-NULL, since a converted NULL leaves the selection.
    Preparation has already checked row identity and compiled both statements. The
    conversion runs only for fetched values during execution, never during preview.
    """

    select_sql: str
    update_sql: str
    column: str
    key_columns: tuple[str, ...]
    conversion: Callable[[Any], Any]
    description: str
    recovery: StatementRecovery | None = None
    snapshot: StoredValueSnapshot | None = None
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.REWRITE_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Read and rewrite each batch once using the prepared conversion."""
        if editor.collect_sql:
            editor.collected_sql.append(STORED_VALUE_REWRITE_PREVIEW.format(description=self.description))
            return
        if self.snapshot is not None:
            await SchemaCleanup(
                SchemaChangePlan((replace(self, select_sql=self.snapshot.select_sql, snapshot=None),)),
                SchemaChangePlan((SchemaStatement(self.snapshot.drop_sql),)),
                acquisition=SchemaStatement(self.snapshot.create_sql),
            ).execute(editor)
            return
        offset = 0
        while True:
            suffix = editor.client.dialect.clauses.get_limit_offset_sql(
                str(STORED_VALUE_REWRITE_BATCH_SIZE), str(offset) if offset else None
            )
            result = await editor.client.execute(f"{self.select_sql}{suffix}", returns_rows=True)
            rows = result.rows
            if not rows:
                return
            parameters = [
                [self.conversion(row[self.column]), *[row[column] for column in self.key_columns]] for row in rows
            ]
            editor.run_statement_count += 1
            await editor.client._execute_many_built(self.update_sql, parameters, recovery=self.recovery)
            if len(rows) < STORED_VALUE_REWRITE_BATCH_SIZE:
                return
            offset += len(rows) if self.column in self.key_columns else sum(row[0] is not None for row in parameters)
