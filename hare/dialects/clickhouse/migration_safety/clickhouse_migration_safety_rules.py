from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.migration_safety.migration_safety_rules import MigrationSafetyRules
from hare.dialects.clickhouse.migration_safety.constants import CLICKHOUSE_TABLE_TOTAL_ROWS_SQL

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class ClickhouseMigrationSafetyRules(MigrationSafetyRules):
    """ClickHouse's migration safety rules - the shared ones; a table's rows are read off
    ``system.tables``, where the table engines keep the count without reading the data."""

    async def count_table_rows(
        self, client: DatabaseClient, table_name: str, schema: str | None, at_most: int
    ) -> int | None:
        # A ClickHouse database is the connection's namespace of tables - there is no schema.
        rows = await client.execute_dicts(CLICKHOUSE_TABLE_TOTAL_ROWS_SQL, [table_name])
        if not rows:
            return 0
        total_rows = rows[0]["total_rows"]
        # An engine keeping no count - a view - counts as large.
        return None if total_rows is None else int(total_rows)
