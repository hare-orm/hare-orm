from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.dialects.postgresql.migration_safety.constants import (
    POSTGRES_UNKNOWN_DEFAULT_RISK,
    POSTGRES_VOLATILE_DEFAULT_RISK,
)
from hare.fields.db_defaults.now import Now
from hare.fields.db_defaults.random_hex import RandomHex
from hare.fields.db_defaults.uuid_v7 import UuidV7
from hare.migrations.operations import AddField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class VolatileDefaultRule(MigrationSafetyRule):
    """Warn when an added database default requires, or may require, rewriting existing rows.

    Built-in declarations carry known semantics: Now is stable; RandomHex and UuidV7 are
    volatile. Raw and custom SQL remain opaque and require review. Their text is neither
    rendered nor parsed to infer volatility, including subclasses overriding known defaults.
    """

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.ADD_FIELD_VOLATILE_DEFAULT

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if not isinstance(operation, AddField) or not context.may_be_large(operation.model_name):
            return None
        field = context.get_field_after(operation.model_name, operation.name)
        if field is None or not field.has_db_default() or not hasattr(field.db_default, "get_sql"):
            return None
        default_type = type(field.db_default)
        if default_type is Now:
            return None
        message = (
            POSTGRES_VOLATILE_DEFAULT_RISK if default_type in (RandomHex, UuidV7) else POSTGRES_UNKNOWN_DEFAULT_RISK
        )
        return context.get_risk(
            self.code,
            message.format(model=operation.model_name, field=operation.name),
            f"Add {operation.name} with null=True and no db_default, set the db_default with AlterField, fill the "
            "existing rows in batches (use a data migration for per-row generated values), then make it NOT NULL "
            "with AlterColumnNotNullSafe.",
        )
