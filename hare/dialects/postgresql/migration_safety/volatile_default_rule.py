from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
from hare.dialects.postgresql.migration_safety.constants import POSTGRES_VOLATILE_DEFAULT_FUNCTIONS_RE
from hare.migrations.operations import AddField
from hare.migrations.safety.enums import MigrationRiskCode

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.safety.migration_safety_context import MigrationSafetyContext


class VolatileDefaultRule(MigrationSafetyRule):
    """A field added to a table that may be large with a database default calling a volatile
    function (``random()``, ``gen_random_uuid()``, ``clock_timestamp()``...): PostgreSQL evaluates it
    for every existing row and rewrites the table, which can't be read or written meanwhile. A
    constant or stable default (``now()``) is stored once, without touching the rows."""

    code: ClassVar[MigrationRiskCode] = MigrationRiskCode.ADD_FIELD_VOLATILE_DEFAULT

    def check_operation(self, context: MigrationSafetyContext) -> MigrationRisk | None:
        operation = context.operation
        if not isinstance(operation, AddField) or not context.may_be_large(operation.model_name):
            return None
        field = context.get_field_after(operation.model_name, operation.name)
        if field is None or not field.has_db_default() or not hasattr(field.db_default, "get_sql"):
            return None
        default_sql = field.db_default.get_sql(context.dialect)
        if POSTGRES_VOLATILE_DEFAULT_FUNCTIONS_RE.search(default_sql) is None:
            return None
        return context.get_risk(
            self.code,
            f"The database default of {operation.model_name}.{operation.name} ({default_sql}) is volatile: the "
            "table is rewritten to fill every row, and can't be read or written meanwhile.",
            f"Add {operation.name} with null=True and no db_default, set the db_default with AlterField, fill the "
            "existing rows in batches with BackfillColumn in a migration with atomic = False, then make it NOT NULL "
            "with AlterColumnNotNullSafe.",
        )
