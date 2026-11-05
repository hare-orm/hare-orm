from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_step import SchemaStep
from hare.dialects.base.schema.tables.table_definition import TableDefinition

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class SchemaChangePlan:
    """Prepared schema work; collection and execution consume the same ordered steps.

    Definitions describe the table before and after a table operation. Catalog
    actions retain an explicit runtime step instead of pretending that its SQL is
    known offline. A step that requires atomicity controls its transaction scope;
    the plan itself does not hide commits around commands that cannot share one.
    """

    steps: tuple[SchemaStep, ...]
    before: TableDefinition | None = None
    after: TableDefinition | None = None

    @property
    def effects(self) -> frozenset[SchemaEffect]:
        """Union of actual step requirements, available before execution."""
        return frozenset(effect for step in self.steps for effect in step.effects)

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Apply each prepared step once, using collection behavior on a preview editor."""
        for step in self.steps:
            await step.execute(editor)
