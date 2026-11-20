from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.dialects.base.schema.plans.enums import SchemaEffect

if TYPE_CHECKING:
    from hare.dialects.base.client.statement_recovery import StatementRecovery
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class SchemaStatement:
    """A fully rendered schema statement or script, unchanged by later model edits."""

    sql: str
    effects: frozenset[SchemaEffect] = frozenset()
    recovery: StatementRecovery | None = None

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Use the editor's dialect execution and schema-cache boundary."""
        if self.recovery is None:
            await editor.run_sql(self.sql)
        else:
            await editor.run_sql(self.sql, recovery=self.recovery)
