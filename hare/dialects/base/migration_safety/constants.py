from __future__ import annotations

#: A table's rows counted up to a limit - reads no further than the limit.
LIMITED_ROW_COUNT_SQL = "SELECT COUNT(*) AS row_count FROM (SELECT 1 FROM {table} LIMIT {limit}) AS limited"
