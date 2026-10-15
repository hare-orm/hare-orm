from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import AlterModelTable, RenameModel
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class RenameModelRule(MigrationSafetyRule):
    """A table renamed - by a renamed model whose table name follows it, or a changed
    ``Meta.table``: the code still running uses the old name and fails until it is replaced."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.RENAME_MODEL
    checks_separated_database_operations: ClassVar[bool] = False

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if isinstance(operation, RenameModel):
            old_name, new_name = operation.old_name, operation.new_name
        elif isinstance(operation, AlterModelTable):
            old_name = new_name = operation.name
        else:
            return None
        old_model = context.get_model_before(old_name)
        new_model = context.get_model_after(new_name)
        if old_model is None or new_model is None:
            return None
        old_table, new_table = old_model._meta.db_table, new_model._meta.db_table
        if old_table == new_table:
            return None
        return context.get_risk(
            self.code,
            f"The table {old_table} becomes {new_table}: the code still running uses {old_table} and fails until "
            "it is replaced.",
            f"Keep the table - set Meta.table = {old_table!r} on the model - or create the new table, copy the "
            "rows, switch the code to it and delete the old one in a later migration.",
        )
