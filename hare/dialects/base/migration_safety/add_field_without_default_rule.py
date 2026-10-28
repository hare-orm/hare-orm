from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import AddField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class AddFieldWithoutDefaultRule(MigrationSafetyRule):
    """A NOT NULL column added with no default at all - neither a Python one nor the database's: the
    database has no value to give the rows the table holds, and the migration fails on any table that
    isn't empty."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.ADD_FIELD_WITHOUT_DEFAULT

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if not isinstance(operation, AddField) or not context.may_hold_rows(operation.model_name):
            return None
        field = context.get_field_after(operation.model_name, operation.name)
        if (
            field is None
            or field.null
            or field.pk
            or field.generated
            or field.has_db_default()
            or field.default is not None
            or context.dialect.schema_editor_class.column_backfill_class.is_auto_now_field(field)
            # A many-to-many relation adds a table, not a column.
            or operation.name in field.model._meta.many_to_many_fields
        ):
            return None
        return context.get_risk(
            self.code,
            f"{operation.model_name}.{operation.name} is NOT NULL with no default: the database has no value "
            "for the rows the table holds, and the migration fails unless the table is empty.",
            f"Give {operation.name} a default or a db_default, or add it with null=True, fill it, then make it "
            "NOT NULL.",
        )
