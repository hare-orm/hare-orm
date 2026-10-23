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

#: The settings of a bulk update whose key set is a subquery of its own constant rows - a replicated
#: table refuses a mutation reading a subquery otherwise, as one replica might read other rows.
CLICKHOUSE_CONSTANT_KEY_SET_MUTATION_SETTINGS_SQL = " SETTINGS allow_nondeterministic_mutations = 1"
#: The keys of the rows of an UPDATE from a list of rows, a column per key element.
CLICKHOUSE_ROW_KEY_COLUMNS_TEMPLATE = "(SELECT {row_keys} FROM (SELECT arrayJoin({rows}) AS row))"

#: A table read as a derived one - every column of it, the computed ones too.
CLICKHOUSE_DERIVED_TABLE_TEMPLATE = (
    "(SELECT * FROM {table} SETTINGS asterisk_include_materialized_columns = 1, asterisk_include_alias_columns = 1)"
)

#: The most rows one bulk update mutation writes: the mutation finds each row's values by scanning
#: the list of rows, and repeats the list in its condition and in each assignment - a longer one costs
#: more per row and outgrows the server's AST limit, which a mutation run in the background keeps.
CLICKHOUSE_MAX_BULK_UPDATE_ROWS = 1000
#: What precedes a JOIN and an IN of a subquery read once and sent to every shard.
CLICKHOUSE_GLOBAL_PREFIX = "GLOBAL "

#: A LIKE pattern computed by an expression - an empty text's containment, %%, written as %, which
#: ClickHouse matches an empty string by.
CLICKHOUSE_EMPTY_CONTAINMENT_PATTERN_SQL = "if({pattern} = '%%', '%', {pattern})"
