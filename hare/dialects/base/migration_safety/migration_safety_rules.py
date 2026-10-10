from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

from hare.dialects.base.migration_safety.add_field_backfill_rule import AddFieldBackfillRule
from hare.dialects.base.migration_safety.alter_field_rewrite_rule import AlterFieldRewriteRule
from hare.dialects.base.migration_safety.delete_model_rule import DeleteModelRule
from hare.dialects.base.migration_safety.remove_field_rule import RemoveFieldRule
from hare.dialects.base.migration_safety.rename_field_rule import RenameFieldRule
from hare.dialects.base.migration_safety.rename_model_rule import RenameModelRule
from hare.dialects.base.migration_safety.run_sql_rule import RunSqlRule
from hare.dialects.base.migration_safety.schema_change_with_run_python_rule import SchemaChangeWithRunPythonRule

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule


class MigrationSafetyRules:
    """The rules of the migration safety check on the dialect's databases, and how it tells a large
    table. This base has the rules every database shares - renamed and removed fields and models,
    raw SQL, rewritten tables, backfilled columns, schema changes beside Python code - and can't
    count a table's rows.
    """

    def __init__(self, dialect: Dialect) -> None:
        """
        Args:
            dialect: The dialect whose migrations are checked.
        """
        self.dialect = dialect

    @cached_property
    def rules(self) -> tuple[MigrationSafetyRule, ...]:
        """The dialect's rules - built on first use."""
        return tuple(self.get_rules())

    def get_rules(self) -> list[MigrationSafetyRule]:
        """Builds the dialect's rules.

        Returns:
            The rules every database shares; a dialect adds its own.
        """
        return [
            AddFieldBackfillRule(),
            AlterFieldRewriteRule(),
            RenameFieldRule(),
            RemoveFieldRule(),
            RenameModelRule(),
            DeleteModelRule(),
            RunSqlRule(),
            SchemaChangeWithRunPythonRule(),
        ]

    async def count_table_rows(
        self, client: DatabaseClient, table_name: str, schema: str | None, at_most: int
    ) -> int | None:
        """Counts a table's rows, or estimates them - enough to tell whether it holds ``at_most``.

        Args:
            client: The connection.
            table_name: The table.
            schema: Its schema.
            at_most: The count past which the exact number doesn't matter.

        Returns:
            The rows, up to ``at_most``; 0 for a table that doesn't exist yet; None when the
            database can't tell - the table counts as large then.
        """
        return None
