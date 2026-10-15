from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.migrations.operations import AddConstraint, AddField, AlterField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class UniqueConstraintIndexRule(MigrationSafetyRule):
    """A unique constraint added to a table that may be large - an ``AddConstraint`` without
    ``using_index``, or a field added or altered with ``unique=True``: its index is built while the
    table's writes wait."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.ADD_UNIQUE_CONSTRAINT_BUILDS_INDEX

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if isinstance(operation, AddConstraint):
            constraint = operation.constraint
            if (
                not isinstance(constraint, UniqueConstraint)
                or operation.using_index is not None
                or not context.may_be_large(operation.model_name)
            ):
                return None
            if constraint.condition is not None:
                safe_alternative = (
                    "Declare it as a unique PartialIndex in Meta.indexes and add it with AddIndex(..., "
                    "concurrently=True) in a migration with atomic = False."
                )
            else:
                name = constraint.name or "<name>"
                safe_alternative = (
                    f"Build its index first, in a migration with atomic = False: AddIndex({operation.model_name!r}, "
                    f"Index(fields={tuple(constraint.fields)!r}, name={name!r}, unique=True), concurrently=True); "
                    f"then AddConstraint({operation.model_name!r}, UniqueConstraint(fields="
                    f"{tuple(constraint.fields)!r}, name={name!r}), using_index={name!r}) takes it over."
                )
            return context.get_risk(
                self.code,
                f"The unique index of the constraint is built while writes to {operation.model_name} wait.",
                safe_alternative,
            )
        if not isinstance(operation, (AddField, AlterField)) or not context.may_be_large(operation.model_name):
            return None
        new_field = context.get_field_after(operation.model_name, operation.name)
        if new_field is None or not new_field.unique or new_field.pk:
            return None
        if isinstance(operation, AlterField):
            old_field = context.get_field_before(operation.model_name, operation.name)
            if old_field is None or old_field.unique:
                return None
        return context.get_risk(
            self.code,
            f"The unique index of {operation.model_name}.{operation.name} is built while writes to the table wait.",
            f"Declare {operation.name} without unique=True here and add a named UniqueConstraint over it instead: "
            "its index built with AddIndex(..., unique=True, concurrently=True) in a migration with atomic = False, "
            "then AddConstraint(..., using_index=<that name>).",
        )
