from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import AddField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class AddFieldBackfillRule(MigrationSafetyRule):
    """A NOT NULL field added to a table that may be large, with only a Python default: the column is
    added nullable, every existing row is updated to the default, then the column becomes NOT NULL -
    all while the migration holds the table."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.ADD_FIELD_BACKFILLS_ROWS

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if not isinstance(operation, AddField) or not context.may_be_large(operation.model_name):
            return None
        field = context.get_field_after(operation.model_name, operation.name)
        if field is None or not context.dialect.schema_editor_class.column_backfill_class.needs_added_column_backfill(
            field
        ):
            return None
        return context.get_risk(
            self.code,
            f"{operation.model_name}.{operation.name} is NOT NULL with only a Python default: every row of the "
            "table is updated to it while the migration holds the table.",
            f"Add {operation.name} with null=True, fill it in batches with BackfillColumn in a migration with "
            f"atomic = False, then make it NOT NULL with AlterColumnNotNullSafe - or give it a constant db_default, "
            "which the database stores without touching the rows.",
        )
