from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.migration_safety.constants import LIMITED_ROW_COUNT_SQL
from hare.dialects.base.migration_safety.migration_safety_rules import MigrationSafetyRules
from hare.dialects.sqlite.constants import SQLITE_TABLE_EXISTS_SQL

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class SqliteMigrationSafetyRules(MigrationSafetyRules):
    """SQLite's migration safety rules - the shared ones; a table's rows are counted, no further than
    needed. SQLite has no lock finer than the whole database, so the PostgreSQL rules about reads
    and writes waiting on one table don't apply - a rewritten table is still found."""

    async def count_table_rows(
        self, client: DatabaseClient, table_name: str, schema: str | None, at_most: int
    ) -> int | None:
        # SQLite has no schemas - a schema-qualified model's table is used unqualified.
        _, found_rows = await client.execute(SQLITE_TABLE_EXISTS_SQL, [table_name])
        if not found_rows:
            return 0
        count_rows = await client.execute_dicts(
            LIMITED_ROW_COUNT_SQL.format(table=self.dialect.literals.quote_identifier(table_name), limit=int(at_most))
        )
        return int(count_rows[0]["row_count"])
