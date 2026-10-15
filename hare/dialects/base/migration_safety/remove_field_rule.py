from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import RemoveField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class RemoveFieldRule(MigrationSafetyRule):
    """A field removed from the models and its column dropped at once: the code still running during
    the deployment selects the column and fails until it is replaced."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.REMOVE_FIELD
    checks_separated_database_operations: ClassVar[bool] = False

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if not isinstance(operation, RemoveField):
            return None
        return context.get_risk(
            self.code,
            f"The column of {operation.model_name}.{operation.name} is dropped while the code still running reads it.",
            f"First remove the field from the models only - SeparateDatabaseAndState(state_operations="
            f"[RemoveField(model_name={operation.model_name!r}, name={operation.name!r})]) - and drop the column "
            "with RunSQL in a later migration, once no running code reads it.",
        )
