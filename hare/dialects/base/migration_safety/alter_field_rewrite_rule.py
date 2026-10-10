from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import AlterField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class AlterFieldRewriteRule(MigrationSafetyRule):
    """A field change the database makes by rewriting a table that may be large - the dialect's
    schema editor tells which (``rewrites_table_on_alter()``); reads and writes wait for it."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.ALTER_FIELD_REWRITES_TABLE

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if not isinstance(operation, AlterField) or not context.may_be_large(operation.model_name):
            return None
        old_model = context.get_model_before(operation.model_name)
        new_model = context.get_model_after(operation.model_name)
        if old_model is None or new_model is None:
            return None
        editor_class = context.dialect.schema_editor_class
        if not editor_class.column_type_changes_class.rewrites_table_on_alter(
            old_model, new_model, operation.name, context.dialect
        ):
            return None
        return context.get_risk(
            self.code,
            f"Changing {operation.model_name}.{operation.name} rewrites the whole table, which can't be read or "
            "written meanwhile.",
            f"Add a new field of the new definition, fill it in batches with BackfillColumn, switch the code to it "
            f"and remove {operation.name} - or make the change while the table is not in use.",
        )
