from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import DeleteModel
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class DeleteModelRule(MigrationSafetyRule):
    """A model deleted with its table: the code still running during the deployment reads the table
    and fails until it is replaced."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.DELETE_MODEL
    checks_separated_database_operations: ClassVar[bool] = False

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if not isinstance(operation, DeleteModel) or operation.state_only:
            return None
        model = context.get_model_before(operation.name)
        if model is None or not model._meta.managed:
            return None
        return context.get_risk(
            self.code,
            f"The table {model._meta.db_table} is dropped while the code still running reads it.",
            f"First remove the model from the models only - DeleteModel({operation.name!r}, state_only=True) - and "
            "drop the table with RunSQL in a later migration, once no running code reads it.",
        )
