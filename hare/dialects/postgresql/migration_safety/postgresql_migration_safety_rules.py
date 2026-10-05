from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.migration_safety.constants import LIMITED_ROW_COUNT_SQL
from hare.dialects.base.migration_safety.migration_safety_rules import MigrationSafetyRules
from hare.dialects.postgresql.migration_safety.check_constraint_validation_rule import CheckConstraintValidationRule
from hare.dialects.postgresql.migration_safety.constants import (
    POSTGRES_TABLE_ROW_ESTIMATE_SQL,
)
from hare.dialects.postgresql.migration_safety.foreign_key_validation_rule import ForeignKeyValidationRule
from hare.dialects.postgresql.migration_safety.index_without_concurrently_rule import IndexWithoutConcurrentlyRule
from hare.dialects.postgresql.migration_safety.set_not_null_rule import SetNotNullRule
from hare.dialects.postgresql.migration_safety.unique_constraint_index_rule import UniqueConstraintIndexRule
from hare.dialects.postgresql.migration_safety.volatile_default_rule import VolatileDefaultRule

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule


class PostgresqlMigrationSafetyRules(MigrationSafetyRules):
    """PostgreSQL's migration safety rules - the shared ones, and those of its locks: indexes built
    without ``CONCURRENTLY``, constraints validated while the table is locked, NOT NULL set by a
    scan, volatile defaults. A table's rows are the planner's estimate, counted when it has none."""

    def get_rules(self) -> list[MigrationSafetyRule]:
        return [
            *super().get_rules(),
            VolatileDefaultRule(),
            IndexWithoutConcurrentlyRule(),
            CheckConstraintValidationRule(),
            ForeignKeyValidationRule(),
            UniqueConstraintIndexRule(),
            SetNotNullRule(),
        ]

    async def count_table_rows(
        self, client: DatabaseClient, table_name: str, schema: str | None, at_most: int
    ) -> int | None:
        literals = self.dialect.literals
        qualified_table = (
            f"{literals.quote_identifier(schema)}.{literals.quote_identifier(table_name)}"
            if schema
            else literals.quote_identifier(table_name)
        )
        rows = await client.execute_dicts(
            POSTGRES_TABLE_ROW_ESTIMATE_SQL.format(table=literals.get_string_literal_sql(qualified_table))
        )
        if not rows:
            return 0
        estimate = rows[0]["estimate"]
        if estimate >= 0:
            return int(estimate)
        # Never analyzed - counted, no further than needed.
        count_rows = await client.execute_dicts(
            LIMITED_ROW_COUNT_SQL.format(table=qualified_table, limit=int(at_most))
        )
        return int(count_rows[0]["row_count"])
