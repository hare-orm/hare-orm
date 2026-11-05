from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class SchemaTransaction:
    """An indivisible schema plan selected by a dialect with transactional DDL.

    Execution uses a transaction or nested savepoint even for direct editor calls.
    Collection consumes the same body without opening a connection. Statement counts
    propagate on failure so the migration runner can report attempted schema work.
    """

    body: SchemaChangePlan

    @property
    def effects(self) -> frozenset[SchemaEffect]:
        """The work performed inside the transaction boundary."""
        return self.body.effects

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Run every step on the pinned transaction connection."""
        if editor.collect_sql:
            await self.body.execute(editor)
            return
        async with editor.client._in_transaction() as connection:
            transaction_editor = type(editor)(connection, atomic=editor.atomic)
            try:
                await self.body.execute(transaction_editor)
            finally:
                editor.run_statement_count += transaction_editor.run_statement_count
