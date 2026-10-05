from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import RunSQL, SQLOperation
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class RunSqlRule(MigrationSafetyRule):
    """Raw SQL: the check can't tell which tables it locks or for how long."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.RUN_SQL

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        if not isinstance(context.operation, (RunSQL, SQLOperation)):
            return None
        return context.get_risk(
            self.code,
            "Raw SQL can't be checked - it may lock or rewrite a table in use.",
            f"Check the statements by hand, then list MigrationRiskCode.{self.code.name} in the migration's "
            "safety_exemptions.",
        )
