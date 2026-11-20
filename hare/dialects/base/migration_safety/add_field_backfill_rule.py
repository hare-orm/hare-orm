from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.migrations.operations import AddField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class AddFieldBackfillRule(MigrationSafetyRule):
    """An added field whose prepared plan fills rows in a potentially large table."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.ADD_FIELD_BACKFILLS_ROWS

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if not isinstance(operation, AddField) or not context.may_be_large(operation.model_name):
            return None
        plan = context.schema_plan
        if plan is None or SchemaEffect.BACKFILL_ROWS not in plan.effects:
            return None
        return context.get_risk(
            self.code,
            f"Adding {operation.model_name}.{operation.name} fills existing rows as part of its prepared schema plan.",
            f"Choose an addition plan without row filling, then fill {operation.name} separately using the "
            "database's supported backfill strategy, or apply this change while the table is not in use.",
        )
