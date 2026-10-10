from __future__ import annotations

from hare.sql.enums import SetOperation

#: The statement a bulk load of rows is reported under to the observers - the rows travel in
#: ClickHouse's binary Native format, in no SQL text.
CLICKHOUSE_BULK_LOAD_STATEMENT_TEMPLATE = "INSERT INTO {table} ({columns}) FORMAT Native"
#: The LIMIT of an OFFSET without a bound - the most rows ClickHouse counts.
CLICKHOUSE_UNBOUNDED_LIMIT_SQL = "18446744073709551615"
#: The set operations dropping repeated rows, written with DISTINCT - ClickHouse refuses a bare UNION
#: and keeps every row of a bare INTERSECT or EXCEPT.
CLICKHOUSE_SET_OPERATION_SQL = {
    SetOperation.UNION: "UNION DISTINCT",
    SetOperation.INTERSECT: "INTERSECT DISTINCT",
    SetOperation.EXCEPT_OF: "EXCEPT DISTINCT",
}
#: The settings a subquery joining tables runs with - the subquery of a mutation's condition runs
#: without the session's settings, and its LEFT JOIN would give a missing row's columns their type's
#: default, not NULL.
CLICKHOUSE_JOINING_SUBQUERY_SETTINGS = {"join_use_nulls": 1}
#: How a rendered condition begins.
CLICKHOUSE_WHERE_PREFIX = " WHERE "
#: The condition of an UPDATE - joined with a true constant, so it is never a negation of its own.
CLICKHOUSE_UPDATE_CONDITION_TEMPLATE = " WHERE ({condition}) AND 1"
