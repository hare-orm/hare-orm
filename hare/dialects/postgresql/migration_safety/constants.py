from __future__ import annotations

POSTGRES_VOLATILE_DEFAULT_RISK = (
    "The database default of {model}.{field} is volatile: the table is rewritten to fill every row, "
    "and can't be read or written meanwhile."
)
POSTGRES_UNKNOWN_DEFAULT_RISK = (
    "The database default of {model}.{field} has unknown volatility: adding the column may rewrite "
    "the table and block reads and writes. Review the SQL and its functions before applying this migration."
)

#: The planner's row estimate of a table, -1 when it was never analyzed; no row for a missing table.
POSTGRES_TABLE_ROW_ESTIMATE_SQL = (
    "SELECT c.reltuples::bigint AS estimate FROM pg_class c WHERE c.oid = to_regclass({table})"
)
