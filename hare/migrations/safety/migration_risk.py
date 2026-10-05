from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.enums import MigrationRiskCode


@dataclass(frozen=True, slots=True)
class MigrationRisk:
    """One risky operation of a migration, found by the migration safety check.

    Attributes:
        code: The rule that found it.
        app_label: The migration's app.
        migration_name: The migration's name.
        operation: The operation, as ``describe()`` writes it.
        message: What goes wrong on a database in use.
        safe_alternative: How to make the same change safely.
        exempted: The migration lists the code in ``safety_exemptions`` - reported, but accepted.
    """

    code: MigrationRiskCode
    app_label: str
    migration_name: str
    operation: str
    message: str
    safe_alternative: str
    exempted: bool = False

    def __str__(self) -> str:
        return f"{self.app_label}.{self.migration_name}: {self.operation} [{self.code}] {self.message}"
