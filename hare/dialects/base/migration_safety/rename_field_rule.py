from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import RenameField
from hare.migrations.safety.enums import MigrationRiskCode
from hare.migrations.state.model_state import ModelState

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class RenameFieldRule(MigrationSafetyRule):
    """A field renamed together with its column: the code still running during the deployment uses
    the old column name and fails until it is replaced."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.RENAME_FIELD
    checks_separated_database_operations: ClassVar[bool] = False

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if not isinstance(operation, RenameField):
            return None
        app_label = context.migration.app_label
        old_model_state = context.state_before.models.get((app_label, operation.model_name))
        new_model_state = context.state_after.models.get((app_label, operation.model_name))
        if old_model_state is None or new_model_state is None:
            return None
        old_column = ModelState.get_field_db_column(operation.old_name, old_model_state.fields[operation.old_name])
        new_column = ModelState.get_field_db_column(operation.new_name, new_model_state.fields[operation.new_name])
        if old_column == new_column:
            return None
        return context.get_risk(
            self.code,
            f"The column {old_column} becomes {new_column}: the code still running uses {old_column} and fails "
            "until it is replaced.",
            f"Keep the column - declare the field with source_field={old_column!r} - or add {operation.new_name}, "
            f"fill it, switch the code to it, and remove {operation.old_name} in a later migration.",
        )
