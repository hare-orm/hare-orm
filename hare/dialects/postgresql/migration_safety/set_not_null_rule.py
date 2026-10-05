from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import AlterColumnNotNullSafe, AlterField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class SetNotNullRule(MigrationSafetyRule):
    """NOT NULL set on a column of a table that may be large by an ``AlterField``, or by an
    ``AlterColumnNotNullSafe`` inside a transaction: the table is scanned while its reads and writes
    wait."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.SET_NOT_NULL_SCANS_TABLE

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        safe_alternative = (
            "Use AlterColumnNotNullSafe in a migration with atomic = False: it validates a CHECK (column IS NOT "
            "NULL) without blocking writes, and PostgreSQL then sets NOT NULL without scanning the table."
        )
        if isinstance(operation, AlterColumnNotNullSafe):
            if not context.migration.atomic or not context.may_be_large(operation.model_name):
                return None
            return context.get_risk(
                self.code,
                f"Inside the migration's transaction {operation.model_name} is scanned for NULLs while writes "
                "wait, then again while it can't be read.",
                safe_alternative,
            )
        if not isinstance(operation, AlterField) or not context.may_be_large(operation.model_name):
            return None
        old_field = context.get_field_before(operation.model_name, operation.name)
        new_field = context.get_field_after(operation.model_name, operation.name)
        if old_field is None or new_field is None or not old_field.null or new_field.null:
            return None
        return context.get_risk(
            self.code,
            f"Setting {operation.model_name}.{operation.name} NOT NULL scans the table while it can't be read or "
            "written.",
            safe_alternative,
        )
