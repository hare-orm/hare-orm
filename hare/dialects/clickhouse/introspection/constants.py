from __future__ import annotations

import re

#: The quotes of ClickHouse SQL - of a string, and of an identifier.
CLICKHOUSE_SQL_QUOTES = frozenset({"'", "`", '"'})
#: A character of an unquoted word of SQL.
CLICKHOUSE_SQL_WORD_PATTERN = re.compile(r"[A-Za-z0-9_]")
#: An identifier the server writes without quotes.
CLICKHOUSE_PLAIN_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: The clauses following a table's engine in ``system.tables.engine_full``, in the server's order.
CLICKHOUSE_ENGINE_CLAUSES = ("PARTITION BY", "PRIMARY KEY", "ORDER BY", "SAMPLE BY", "TTL", "SETTINGS")
CLICKHOUSE_TTL_CLAUSE = "TTL"
CLICKHOUSE_SETTINGS_CLAUSE = "SETTINGS"
#: The clauses of a materialized view's definition before its query, in the server's order.
CLICKHOUSE_MATERIALIZED_VIEW_CLAUSES = ("REFRESH", "DEPENDS ON", "APPEND", "TO", "ENGINE", "DEFINER", "SQL SECURITY")
CLICKHOUSE_REFRESH_CLAUSE = "REFRESH"
CLICKHOUSE_DEPENDS_ON_CLAUSE = "DEPENDS ON"
CLICKHOUSE_APPEND_CLAUSE = "APPEND"
CLICKHOUSE_TO_CLAUSE = "TO"
CLICKHOUSE_ENGINE_CLAUSE = "ENGINE"
CLICKHOUSE_ORDER_BY_CLAUSE = "ORDER BY"
CLICKHOUSE_PARTITION_BY_CLAUSE = "PARTITION BY"
#: The word a view's query follows in its definition.
CLICKHOUSE_VIEW_QUERY_WORD = "AS"
#: How the server writes the sort of a table sorted by nothing.
CLICKHOUSE_NO_SORT_SQL = "tuple()"
#: How ``system.tables`` names the engines of a view and of a materialized view.
CLICKHOUSE_VIEW_ENGINE = "View"
CLICKHOUSE_MATERIALIZED_VIEW_ENGINE = "MaterializedView"
#: The views of the names given, with their queries and their whole definitions.
CLICKHOUSE_VIEWS_SQL = (
    "SELECT name, engine, as_select, create_table_query FROM system.tables WHERE database = $1 AND name IN ({names})"
)
#: A query as the server reads it - each name of it found: two spellings of one query read the same.
CLICKHOUSE_QUERY_TREE_SQL = "EXPLAIN QUERY TREE dump_ast = 1 {query}"
#: The plural ending of a unit in a refresh schedule.
CLICKHOUSE_SCHEDULE_PLURAL_PATTERN = re.compile(r"(?<=[A-Z])S\b")
#: The dictionaries of the names given - their keys and attributes, with their definitions: a
#: dictionary not loaded yet shows neither its layout nor its lifetime elsewhere.
CLICKHOUSE_DICTIONARIES_SQL = (
    "SELECT dictionaries.name AS name, dictionaries.key.names AS key_names, "
    "dictionaries.attribute.names AS attribute_names, tables.create_table_query AS create_table_query "
    "FROM system.dictionaries AS dictionaries INNER JOIN system.tables AS tables "
    "ON tables.database = dictionaries.database AND tables.name = dictionaries.name "
    "WHERE dictionaries.database = $1 AND dictionaries.name IN ({names})"
)
#: The clauses of a dictionary's definition naming its source, its layout and its lifetime.
CLICKHOUSE_DICTIONARY_LIFETIME_CLAUSE = "LIFETIME"
#: A number of seconds in a dictionary's lifetime.
CLICKHOUSE_DICTIONARY_SECONDS_PATTERN = re.compile(r"\d+")
CLICKHOUSE_DICTIONARY_SOURCE_CLAUSE = "SOURCE"
CLICKHOUSE_DICTIONARY_LAYOUT_CLAUSE = "LAYOUT"
#: A password of a dictionary's source.
CLICKHOUSE_DICTIONARY_PASSWORD_PATTERN = re.compile(r"PASSWORD\s+'(?:[^'\\]|\\.)*'", re.IGNORECASE)
#: How a projection begins in the column list of a table's ``CREATE TABLE``.
CLICKHOUSE_PROJECTION_DEFINITION_PREFIX = "PROJECTION "
#: How ``system.columns`` wraps the codecs of a column.
CLICKHOUSE_CODEC_PREFIX = "CODEC("
#: A character a string of the server escapes.
CLICKHOUSE_ESCAPED_CHARACTER_PATTERN = re.compile(r"\\(.)")
#: A whole number as a table setting holds it.
CLICKHOUSE_INTEGER_SETTING_PATTERN = re.compile(r"-?\d+")
#: The server's own values of the table settings named - a table setting equal to it changes nothing.
CLICKHOUSE_TABLE_SETTING_DEFAULTS_SQL = "SELECT name, value FROM system.merge_tree_settings WHERE name IN ({names})"
#: A statement as the server writes it; NULL for one it can't read.
CLICKHOUSE_FORMATTED_QUERY_SQL = "formatQuerySingleLineOrNull({parameter})"
#: The statements two spellings of a table option are compared in - the server writes both its own way.
CLICKHOUSE_EXPRESSION_COMPARISON_SQL = "SELECT {sql}"
CLICKHOUSE_TTL_COMPARISON_SQL = "ALTER TABLE compared MODIFY TTL {sql}"
CLICKHOUSE_PROJECTION_COMPARISON_SQL = "ALTER TABLE compared ADD PROJECTION compared ({sql})"
