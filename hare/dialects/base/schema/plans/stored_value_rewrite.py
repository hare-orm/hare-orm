from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.schema.constants import STORED_VALUE_REWRITE_BATCH_SIZE, STORED_VALUE_REWRITE_PREVIEW
from hare.dialects.base.schema.plans.enums import SchemaEffect

if TYPE_CHECKING:
    from hare.dialects.base.client.statement_recovery import StatementRecovery
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class StoredValueRewrite:
    """Prepared Python conversion of stored values in primary-key ordered batches.

    The key columns remain unchanged, so offsets preserve the existing batch contract.
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
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.REWRITE_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Read and rewrite each batch once using the prepared conversion."""
        if editor.collect_sql:
            editor.collected_sql.append(STORED_VALUE_REWRITE_PREVIEW.format(description=self.description))
            return
        offset = 0
        while True:
            suffix = editor.client.dialect.clauses.get_limit_offset_sql(
                str(STORED_VALUE_REWRITE_BATCH_SIZE), str(offset) if offset else None
            )
            result = await editor.client.execute(f"{self.select_sql}{suffix}", returns_rows=True)
            if not result.rows:
                return
            parameters = [
                [self.conversion(row[self.column]), *[row[column] for column in self.key_columns]]
                for row in result.rows
            ]
            editor.run_statement_count += 1
            await editor.client.execute_many(self.update_sql, parameters, recovery=self.recovery)
            if len(result.rows) < STORED_VALUE_REWRITE_BATCH_SIZE:
                return
            offset += len(result.rows)
