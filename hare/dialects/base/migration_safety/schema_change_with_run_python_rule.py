from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import RunPython
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class SchemaChangeWithRunPythonRule(MigrationSafetyRule):
    """Python code and schema changes of a table that may be large in one atomic migration, on a
    database whose schema changes are transactional: the locks the schema changes take are held
    until the transaction ends - for as long as the code runs."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.SCHEMA_CHANGE_WITH_RUN_PYTHON

    def check_migration(self, contexts: list[MigrationSafetyContext]) -> list[MigrationRisk]:
        if not contexts:
            return []
        first_context = contexts[0]
        if not first_context.migration.atomic or not first_context.features.can_rollback_ddl:
            return []
        run_python_contexts = [context for context in contexts if isinstance(context.operation, RunPython)]
        if not run_python_contexts:
            return []
        changed_model_names = sorted(
            {
                model_name
                for context in contexts
                if not isinstance(context.operation, RunPython)
                for model_name in context.operation.get_table_model_names()
                if context.may_be_large(model_name)
            }
        )
        if not changed_model_names:
            return []
        return [
            context.get_risk(
                self.code,
                "The Python code runs in the transaction that changes the schema of "
                f"{', '.join(changed_model_names)}: their locks are held until the code ends.",
                "Move the RunPython into a migration of its own, or set atomic = False on this migration.",
            )
            for context in run_python_contexts
        ]
