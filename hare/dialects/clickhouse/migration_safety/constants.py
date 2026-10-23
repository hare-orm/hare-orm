from __future__ import annotations

#: A table's rows as its engine keeps them - NULL for an engine that keeps no count (a view); no row
#: for a table that doesn't exist. ``{database}`` is the connection's database (``currentDatabase()``)
#: or the parameter of the model's.
CLICKHOUSE_TABLE_TOTAL_ROWS_SQL = "SELECT total_rows FROM system.tables WHERE database = {database} AND name = $1"
#: The ``{database}`` of ``CLICKHOUSE_TABLE_TOTAL_ROWS_SQL`` for a table of the connection's database.
CLICKHOUSE_CURRENT_DATABASE_SQL = "currentDatabase()"
#: The ``{database}`` of ``CLICKHOUSE_TABLE_TOTAL_ROWS_SQL`` for a table of the model's database.
CLICKHOUSE_DATABASE_PARAMETER_SQL = "$2"
