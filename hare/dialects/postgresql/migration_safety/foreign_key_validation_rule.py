from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.migrations.operations import AddField, AlterField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class ForeignKeyValidationRule(MigrationSafetyRule):
    """A FOREIGN KEY added to a table that may be large - a relation field added without
    ``not_valid=True``, or altered to get a constraint or another target: every row is checked while
    the writes to both tables wait."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.ADD_FOREIGN_KEY_VALIDATES_ROWS

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if not isinstance(operation, (AddField, AlterField)) or not context.may_be_large(operation.model_name):
            return None
        new_field = context.get_field_after(operation.model_name, operation.name)
        if not self.makes_foreign_key(new_field, context):
            return None
        if isinstance(operation, AddField):
            if operation.not_valid:
                return None
            return context.get_risk(
                self.code,
                f"Every row of {operation.model_name} is checked against the new FOREIGN KEY of {operation.name} "
                "while writes to both tables wait.",
                "Pass not_valid=True to AddField - only new and updated rows are checked - and check the existing "
                "ones with ValidateConstraint(<model>, <the foreign key's name>) in a later migration: it doesn't "
                "block writes.",
            )
        old_field = context.get_field_before(operation.model_name, operation.name)
        if (
            self.makes_foreign_key(old_field, context)
            and isinstance(old_field, ForeignKeyFieldInstance)
            and isinstance(new_field, ForeignKeyFieldInstance)
            and old_field.model_name == new_field.model_name
            and old_field.to_field == new_field.to_field
        ):
            return None
        return context.get_risk(
            self.code,
            f"Every row of {operation.model_name} is checked against the FOREIGN KEY {operation.name} gets, while "
            "writes to both tables wait.",
            "Make the change in a SeparateDatabaseAndState: the AlterField as a state operation, and as database "
            "operations RunSQL adding the constraint NOT VALID, then ValidateConstraint in a later migration.",
        )

    @staticmethod
    def makes_foreign_key(field: Field[Any] | None, context: MigrationSafetyContext) -> bool:
        """Whether a field is a relation the database gets a FOREIGN KEY for.

        Args:
            field: The field, None for a missing one.
            context: The operation's context.

        Returns:
            True for a ForeignKeyField/OneToOneField with ``db_constraint`` on a database with
            foreign keys.
        """
        return (
            isinstance(field, ForeignKeyFieldInstance)
            and bool(field.db_constraint)
            and context.features.supports_foreign_keys
        )
