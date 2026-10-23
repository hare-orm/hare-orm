from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.migration_safety.migration_safety_rules import MigrationSafetyRules
from hare.dialects.clickhouse.migration_safety.constants import (
    CLICKHOUSE_CURRENT_DATABASE_SQL,
    CLICKHOUSE_DATABASE_PARAMETER_SQL,
    CLICKHOUSE_TABLE_TOTAL_ROWS_SQL,
)

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class ClickhouseMigrationSafetyRules(MigrationSafetyRules):
    """ClickHouse's migration safety rules - the shared ones; a table's rows are read off
    ``system.tables``, where the table engines keep the count without reading the data."""

    async def count_table_rows(
        self, client: DatabaseClient, table_name: str, schema: str | None, at_most: int
    ) -> int | None:
        # A model's schema is a database of the server - its table is looked for there.
        if schema:
            sql = CLICKHOUSE_TABLE_TOTAL_ROWS_SQL.format(database=CLICKHOUSE_DATABASE_PARAMETER_SQL)
            rows = await client.execute_dicts(sql, [table_name, schema])
        else:
            sql = CLICKHOUSE_TABLE_TOTAL_ROWS_SQL.format(database=CLICKHOUSE_CURRENT_DATABASE_SQL)
            rows = await client.execute_dicts(sql, [table_name])
        if not rows:
            return 0
        total_rows = rows[0]["total_rows"]
        # An engine keeping no count - a view - counts as large.
        return None if total_rows is None else int(total_rows)
