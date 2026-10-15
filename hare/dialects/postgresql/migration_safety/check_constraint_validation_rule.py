from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import AddConstraint
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class CheckConstraintValidationRule(MigrationSafetyRule):
    """A CHECK constraint added to a table that may be large without ``not_valid=True``: every row is
    checked while the table's reads and writes wait."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.ADD_CHECK_CONSTRAINT_VALIDATES_ROWS

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if (
            not isinstance(operation, AddConstraint)
            or not isinstance(operation.constraint, CheckConstraint)
            or operation.not_valid
            or not context.may_be_large(operation.model_name)
        ):
            return None
        return context.get_risk(
            self.code,
            f"Every row of {operation.model_name} is checked while the table can't be read or written.",
            f"Pass not_valid=True - only new and updated rows are checked - and check the existing ones with "
            f"ValidateConstraint({operation.model_name!r}, {operation.constraint.name!r}) in a later migration: "
            "it doesn't block writes.",
        )
