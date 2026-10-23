from __future__ import annotations

import re

#: The quotes of ClickHouse SQL - of a string, and of an identifier.
CLICKHOUSE_SQL_QUOTES = frozenset({"'", "`", '"'})
#: A character of an unquoted word of SQL.
CLICKHOUSE_SQL_WORD_PATTERN = re.compile(r"[A-Za-z0-9_]")
#: The character each backslash escape of a quoted text stands for - any other escaped character is itself.
CLICKHOUSE_STRING_UNESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "f": "\f", "a": "\a", "v": "\v"}
#: An identifier the server writes without quotes.
CLICKHOUSE_PLAIN_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: The clauses following a table's engine in ``system.tables.engine_full``, in the server's order.
CLICKHOUSE_ENGINE_CLAUSES = ("PARTITION BY", "PRIMARY KEY", "ORDER BY", "SAMPLE BY", "TTL", "SETTINGS")
CLICKHOUSE_TTL_CLAUSE = "TTL"
CLICKHOUSE_SETTINGS_CLAUSE = "SETTINGS"
CLICKHOUSE_ORDER_BY_CLAUSE = "ORDER BY"
#: How the server writes the sort of a table sorted by nothing.
CLICKHOUSE_NO_SORT_SQL = "tuple()"
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
#: What a constraint's definition in a table's column list begins with.
CLICKHOUSE_CONSTRAINT_DEFINITION_PREFIX = "CONSTRAINT "
#: What a CHECK constraint's condition follows, after its name.
CLICKHOUSE_CHECK_CONSTRAINT_PREFIX = "CHECK "


#: The engine of a table, with its arguments.
CLICKHOUSE_TABLE_ENGINE_SQL = "SELECT engine, engine_full FROM system.tables WHERE database = $1 AND name = $2"


#: The engine whose last argument is a version column the server appends to the table's sorting key - its
#: replicated and shared variants end with the name too.
CLICKHOUSE_VERSIONED_COLLAPSING_ENGINE_NAME = "VersionedCollapsingMergeTree"
#: A string literal as the server writes it - a backslash escapes the character after it.
CLICKHOUSE_QUOTED_STRING_PATTERN = re.compile(r"'((?:[^'\\]|\\.)*)'", re.DOTALL)
#: An escape of a string literal - a byte by its hex digits, or one character.
CLICKHOUSE_STRING_ESCAPE_PATTERN = re.compile(r"\\(x[0-9a-fA-F]{2}|.)", re.DOTALL)
#: The characters the escapes of a string literal stand for - any other escaped character stands for itself.
CLICKHOUSE_ESCAPED_CHARACTERS = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
    "b": "\b",
    "f": "\f",
    "a": "\a",
    "v": "\v",
}

#: The engine of a table reading the tables of a cluster, and how a model names its own database in it.
CLICKHOUSE_DISTRIBUTED_ENGINE_NAME = "Distributed"
CLICKHOUSE_CURRENT_DATABASE_SQL_FUNCTION = "currentDatabase()"

#: The typed literals hare writes a column default as, which the server keeps as written: a float by its
#: eight (or four) bytes, a decimal of its own scale, a UUID or an address from its text, and a cast to a
#: ``Dynamic`` or ``Variant`` type.
CLICKHOUSE_FLOAT_DEFAULT_PATTERN = re.compile(r"reinterpretAsFloat(32|64)\(unhex\('([0-9a-fA-F]+)'\)\)")
CLICKHOUSE_DECIMAL_DEFAULT_PATTERN = re.compile(r"toDecimal(?:32|64|128|256)\('(-?[0-9]+(?:\.[0-9]+)?)', *[0-9]+\)")
CLICKHOUSE_TEXT_VALUE_DEFAULT_PATTERN = re.compile(r"to(UUID|IPv4|IPv6)\('([^'\\]*)'\)")
CLICKHOUSE_CAST_DEFAULT_PATTERN = re.compile(r"CAST\((.+), *'[^']*'\)")
