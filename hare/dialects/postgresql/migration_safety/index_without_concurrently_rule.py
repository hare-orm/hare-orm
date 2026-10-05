from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import AddField, AddIndex, AlterField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class IndexWithoutConcurrentlyRule(MigrationSafetyRule):
    """An index built on a table that may be large while its writes wait - an ``AddIndex`` without
    ``concurrently=True``, or the index of a field added or altered with ``db_index=True``."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.ADD_INDEX_WITHOUT_CONCURRENTLY

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if isinstance(operation, AddIndex):
            if operation.concurrently or not context.may_be_large(operation.model_name):
                return None
            return context.get_risk(
                self.code,
                f"The index is built while writes to {operation.model_name} wait.",
                "Pass concurrently=True and set atomic = False on the migration - CREATE INDEX CONCURRENTLY "
                "builds it while the table is written to.",
            )
        if not isinstance(operation, (AddField, AlterField)) or not context.may_be_large(operation.model_name):
            return None
        new_field = context.get_field_after(operation.model_name, operation.name)
        if new_field is None or not new_field.index or new_field.pk or new_field.unique:
            return None
        if isinstance(operation, AlterField):
            old_field = context.get_field_before(operation.model_name, operation.name)
            if old_field is None or old_field.index:
                return None
        return context.get_risk(
            self.code,
            f"The index of {operation.model_name}.{operation.name} is built while writes to the table wait.",
            f"Declare {operation.name} without db_index=True here, then add the index in a migration with atomic = "
            "False: AddIndex(..., concurrently=True) - and keep it in Meta.indexes.",
        )
