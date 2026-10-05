from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.enums import MigrationRiskCode
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class MigrationSafetyRule:
    """One rule of the migration safety check: what makes an operation - or a whole migration -
    risky on a database in use, and how to make the same change safely.

    Attributes:
        code: The code of the risks the rule finds.
        checks_separated_database_operations: Whether it also checks the database operations of a
            ``SeparateDatabaseAndState`` - False for a rule about the running code, which such an
            operation's author keeps in step with the database.
    """

    code: ClassVar[MigrationRiskCode]
    checks_separated_database_operations: ClassVar[bool] = True

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        """Checks one operation - nothing by default.

        Args:
            context: The operation and what is known about it.

        Returns:
            Its risk, None when there is none.
        """
        return None

    def check_migration(self, contexts: list[MigrationSafetyContext]) -> list[MigrationRisk]:
        """Checks a whole migration - nothing by default.

        Args:
            contexts: One context per top-level operation of the migration, in order.

        Returns:
            Its risks.
        """
        return []
