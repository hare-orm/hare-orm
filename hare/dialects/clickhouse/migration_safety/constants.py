from __future__ import annotations

#: A table's rows as its engine keeps them - NULL for an engine that keeps no count (a view); no row
#: for a table that doesn't exist.
CLICKHOUSE_TABLE_TOTAL_ROWS_SQL = (
    "SELECT total_rows FROM system.tables WHERE database = currentDatabase() AND name = $1"
)
