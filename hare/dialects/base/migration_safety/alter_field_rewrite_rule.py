from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.migrations.operations import AlterField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class AlterFieldRewriteRule(MigrationSafetyRule):
    """A field change the database makes by rewriting a table that may be large - the dialect's
    prepared schema plan identifies the row-rewriting steps."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.ALTER_FIELD_REWRITES_TABLE

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if not isinstance(operation, AlterField) or not context.may_be_large(operation.model_name):
            return None
        plan = context.schema_plan
        if plan is None or SchemaEffect.REWRITE_ROWS not in plan.effects:
            return None
        return context.get_risk(
            self.code,
            f"Changing {operation.model_name}.{operation.name} rewrites stored rows "
            "as part of its prepared schema plan.",
            f"Add a new field of the new definition, fill it in batches with BackfillColumn, switch the code to it "
            f"and remove {operation.name} - or make the change while the table is not in use.",
        )
