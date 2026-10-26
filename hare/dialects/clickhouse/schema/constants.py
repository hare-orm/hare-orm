from __future__ import annotations

import re

from hare.sql.order import Order

#: The fields a ClickHouse container type maps to.
CLICKHOUSE_ARRAY_FIELD_PATH = "hare.fields.data.containers.ArrayField"
CLICKHOUSE_MAP_FIELD_PATH = "hare.fields.data.containers.MapField"
CLICKHOUSE_TUPLE_FIELD_PATH = "hare.fields.data.containers.TupleField"
CLICKHOUSE_NESTED_FIELD_PATH = "hare.fields.data.containers.NestedField"
#: The field a type no field maps to is read as - its values as text.
CLICKHOUSE_FALLBACK_FIELD_PATH = "hare.fields.data.text.TextField"
#: The field of a dictionary-encoded type.
CLICKHOUSE_LOW_CARDINALITY_FIELD_PATH = "hare.dialects.clickhouse.fields.LowCardinalityField"
#: The fields of an enum: of one numbered as a ``CharEnumField`` numbers its values, of one whose labels
#: are member names, and of the labels of any other.
CLICKHOUSE_CHAR_ENUM_FIELD_PATH = "hare.fields.data.choices.CharEnumField"
CLICKHOUSE_INT_ENUM_FIELD_PATH = "hare.fields.data.choices.IntEnumField"
CLICKHOUSE_ENUM_LABEL_FIELD_PATH = "hare.fields.data.text.CharField"
#: The name an enum class read off a column type is given after - its column's name follows it.
CLICKHOUSE_INSPECTED_ENUM_NAME = "labels"
#: The fields of a value of any type and of a value of one of several types.
CLICKHOUSE_DYNAMIC_FIELD_PATH = "hare.dialects.clickhouse.fields.DynamicField"
CLICKHOUSE_VARIANT_FIELD_PATH = "hare.dialects.clickhouse.fields.VariantField"
#: How the most types a ``Dynamic`` column keeps apart is named in its type.
CLICKHOUSE_DYNAMIC_MAX_TYPES_ARGUMENT = "max_types="
#: The codecs of a column as ``CODEC(...)`` takes them - names with their arguments, comma-separated.
CLICKHOUSE_CODEC_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*(?:\([A-Za-z0-9_, ]*\))?(?:\s*,\s*[A-Za-z][A-Za-z0-9_]*(?:\([A-Za-z0-9_, ]*\))?)*"
)
#: The table settings a table with projections takes a lightweight DELETE and the merges of an engine
#: dropping rows (``ReplacingMergeTree``, ...) by, and their value - the projections of the parts the
#: rows leave are built again.
CLICKHOUSE_PROJECTION_REBUILD_SETTINGS = ("lightweight_mutation_projection_mode", "deduplicate_merge_projection_mode")
CLICKHOUSE_PROJECTION_REBUILD_MODE = "rebuild"
#: A view - a table of the ``View`` engine.
CLICKHOUSE_VIEW_CREATE_TEMPLATE = "CREATE {or_replace}VIEW {view} AS\n{query};"
#: A view of either type is dropped and renamed as a table.
CLICKHOUSE_VIEW_DROP_TEMPLATE = "DROP VIEW {if_exists}{view};"
CLICKHOUSE_VIEW_RENAME_TEMPLATE = "RENAME TABLE {view} TO {new_view};"
#: A materialized view - refreshed on a schedule or following the inserts of the table it reads,
#: writing to a table or keeping a storage of its own.
CLICKHOUSE_MATERIALIZED_VIEW_CREATE_TEMPLATE = (
    "CREATE MATERIALIZED VIEW {if_not_exists}{view}{schedule}{storage}{empty} AS\n{query};"
)
#: The settings of a materialized view's own sorted storage - its columns are its query's, and a
#: nullable one may be a key: ``unique_columns`` of a column holding NULL.
CLICKHOUSE_MATERIALIZED_VIEW_STORAGE_SETTINGS_SQL = " SETTINGS allow_nullable_key = 1"
#: The rows of a materialized view's query written into it.
CLICKHOUSE_MATERIALIZED_VIEW_FILL_TEMPLATE = "INSERT INTO {view}\n{query};"
#: A fill of a materialized view on a cluster from tables keeping rows of each server's own.
CLICKHOUSE_MATERIALIZED_VIEW_CLUSTER_FILL_MESSAGE = (
    "{model}: the materialized view {view} is filled by its query on the server the connection is to, over the "
    "rows of that server alone - its query reads {read_tables}, not a distributed table. Read distributed tables "
    "(and name them in read_columns for raw SQL), or declare the view with_data=False and fill it on every server"
)
#: The query and the schedule of a materialized view changed in place.
CLICKHOUSE_MATERIALIZED_VIEW_QUERY_TEMPLATE = "ALTER TABLE {view} MODIFY QUERY\n{query};"
CLICKHOUSE_MATERIALIZED_VIEW_SCHEDULE_TEMPLATE = "ALTER TABLE {view} MODIFY REFRESH {schedule};"
#: A refresh of a view refreshed on a schedule started now, and waited for.
CLICKHOUSE_VIEW_REFRESH_TEMPLATE = "SYSTEM REFRESH VIEW {view};"
CLICKHOUSE_VIEW_WAIT_TEMPLATE = "SYSTEM WAIT VIEW {view};"
CLICKHOUSE_TABLE_TRUNCATE_TEMPLATE = "TRUNCATE TABLE {table};"
#: The schedule of a refreshed view as ``REFRESH`` takes it - ``EVERY 1 DAY OFFSET 2 HOUR``.
CLICKHOUSE_REFRESH_SCHEDULE_PATTERN = re.compile(
    r"(?:EVERY|AFTER)(?: \d+ (?:SECOND|MINUTE|HOUR|DAY|WEEK|MONTH|YEAR)S?)+"
    r"(?: OFFSET(?: \d+ (?:SECOND|MINUTE|HOUR|DAY|WEEK|MONTH|YEAR)S?)+)?"
    r"(?: RANDOMIZE FOR(?: \d+ (?:SECOND|MINUTE|HOUR|DAY|WEEK|MONTH|YEAR)S?)+)?",
    re.IGNORECASE,
)
#: A dictionary - its columns, its key, where it is loaded from, how it is kept and for how long.
CLICKHOUSE_DICTIONARY_CREATE_TEMPLATE = (
    "CREATE {or_replace}DICTIONARY {exists}{dictionary} ({columns}) PRIMARY KEY {key} SOURCE({source}) "
    "LAYOUT({layout}){lifetime};"
)
#: How long a dictionary keeps its rows before loading them again.
CLICKHOUSE_DICTIONARY_LIFETIME_TEMPLATE = " LIFETIME(MIN {lifetime_minimum} MAX {lifetime_maximum})"
#: The layouts that keep no rows - each lookup reads the source - and so take no ``LIFETIME``.
CLICKHOUSE_DICTIONARY_DIRECT_LAYOUT_NAMES = frozenset({"DIRECT", "COMPLEX_KEY_DIRECT"})
CLICKHOUSE_DICTIONARY_DROP_TEMPLATE = "DROP DICTIONARY {if_exists}{dictionary};"
CLICKHOUSE_DICTIONARY_RENAME_TEMPLATE = "RENAME DICTIONARY {dictionary} TO {new_dictionary};"
CLICKHOUSE_DICTIONARY_RELOAD_TEMPLATE = "SYSTEM RELOAD DICTIONARY {dictionary};"
#: The source of a dictionary of a model's own table - read by the server from itself, as the user of
#: the connection: without one it reads as the default user with no password.
CLICKHOUSE_DICTIONARY_TABLE_SOURCE_TEMPLATE = "CLICKHOUSE(DB {database} TABLE {table} USER {user} PASSWORD {password})"
#: What stands for the password in a dictionary's SQL that is shown, not run.
CLICKHOUSE_DICTIONARY_HIDDEN_PASSWORD = "[HIDDEN]"  # nosec B105
#: An environment variable a dictionary's source reads when the dictionary is created.
CLICKHOUSE_DICTIONARY_ENVIRONMENT_PATTERN = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
#: The layout of a dictionary keyed by fields of any types.
CLICKHOUSE_DICTIONARY_DEFAULT_LAYOUT = "COMPLEX_KEY_HASHED()"
#: A dictionary's layout with its arguments - ``CACHE(SIZE_IN_CELLS 1000)``.
CLICKHOUSE_DICTIONARY_LAYOUT_PATTERN = re.compile(r"[A-Za-z_]+\([A-Za-z0-9_ '/.\-]*\)")
#: The longest a dictionary waits to be loaded again, in seconds - ten years.
CLICKHOUSE_DICTIONARY_LIFETIME_LIMIT = 315_360_000
#: How the engines of the MergeTree family end, and the table options of theirs alone.
CLICKHOUSE_MERGE_TREE_ENGINE_SUFFIX = "MergeTree"
CLICKHOUSE_MERGE_TREE_OPTIONS = (
    "order_by",
    "sample_by",
    "partition_by",
    "ttl",
    "settings",
    "column_ttls",
    "projections",
    "lightweight_updates",
)
#: The table settings a table changed by lightweight UPDATEs takes - the number of each row's block and
#: its place in it, which the new values written beside a row are matched to it by.
CLICKHOUSE_LIGHTWEIGHT_UPDATE_SETTINGS = ("enable_block_number_column", "enable_block_offset_column")
CLICKHOUSE_LIGHTWEIGHT_UPDATE_SETTING_VALUE = 1
#: The table setting letting a column of the sorting or the partition key hold NULL - ClickHouse refuses
#: a Nullable key column without it.
CLICKHOUSE_ALLOW_NULLABLE_KEY_SETTING = "allow_nullable_key"
#: The table options ClickHouse changes with an ``ALTER TABLE`` - a change of any other one remakes the
#: table.
CLICKHOUSE_ALTERABLE_TABLE_OPTIONS = frozenset(
    {"ttl", "settings", "column_codecs", "column_ttls", "projections", "lightweight_updates"}
)
#: The table settings set when the table is created and never after.
CLICKHOUSE_CREATION_ONLY_TABLE_SETTINGS = frozenset({"index_granularity", "index_granularity_bytes"})

#: Writes the default of a column just added into the rows stored before it - they would read the
#: column's default as declared at the time of reading.
CLICKHOUSE_MATERIALIZE_COLUMN_TEMPLATE = "ALTER TABLE {table} MATERIALIZE COLUMN {column}"
#: A column made NOT NULL with the DEFAULT its NULL rows would take - ClickHouse 25.12+ refuses the change
#: of a column that has none.
CLICKHOUSE_NOT_NULL_WITH_DEFAULT_TEMPLATE = "MODIFY COLUMN {column} {sql_type} DEFAULT {default}"
#: The empty value of a type - the default of a column made NOT NULL that declares none, dropped again.
CLICKHOUSE_TYPE_EMPTY_VALUE_SQL = "defaultValueOfTypeName({type_name})"
#: Removes the rows a lightweight DELETE only marked - a MODIFY COLUMN converts them too, and fails on a
#: value of a deleted row the new type can't hold.
CLICKHOUSE_APPLY_DELETED_MASK_TEMPLATE = "ALTER TABLE {table} APPLY DELETED MASK"

#: The start of a refresh schedule a view's dependencies need - ClickHouse refreshes a view of
#: ``REFRESH AFTER`` on its own.
CLICKHOUSE_REFRESH_EVERY_KEYWORD = "EVERY"
#: A whole number as a Decimal256 - a bound a Decimal or a float column is compared with; a Decimal
#: compared with an Int64 bound past its own digits raises DECIMAL_OVERFLOW.
CLICKHOUSE_WIDE_DECIMAL_BOUND_TEMPLATE = "toDecimal256('{}', 0)"
#: A column made NOT NULL while it holds NULL - ClickHouse would write its type's default in place of
#: each.
CLICKHOUSE_NOT_NULL_CHANGE_REFUSED_MESSAGE = "column {column} of table {table} holds NULL"
#: The text a FixedString column holds - its bytes without the zero bytes padding it to its width.
CLICKHOUSE_FIXED_STRING_TEXT_TEMPLATE = "replaceRegexpOne(toString({column}), '\\\\x00+$', '')"
#: A FixedString column turned into a String of its text - the step a narrower FixedString is reached by.
CLICKHOUSE_FIXED_STRING_TO_TEXT_TEMPLATE = "MODIFY COLUMN {column} {sql_type}, UPDATE {column} = {text} WHERE 1"
#: The type of a text column.
CLICKHOUSE_TEXT_TYPE_NAME = "String"
#: The name a check's lambda gives an element of an array - numbered by its depth.
CLICKHOUSE_HELD_ELEMENT_NAME = "hare_element"
#: A text column's type - a type change from it parses each value, and a value that doesn't parse fails
#: the change's mutation after the column took its new type, or becomes NULL in a Nullable one.
CLICKHOUSE_TEXT_COLUMN_TYPE_PATTERN = r"(?:Nullable\(|LowCardinality\()*(?:String|FixedString\(\d+\))\)*"
#: The type a column has and the server's name of the type it changes to - a field's type is declared
#: by its SQL name (``INT``, ``VARCHAR(40)``); no row is read of an empty table.
CLICKHOUSE_COLUMN_TYPES_QUERY = (
    "SELECT toTypeName({column}) AS old_type, toTypeName(defaultValueOfTypeName({new_type})) AS new_type "
    "FROM {table} LIMIT 1"
)
#: The precision and scale of a Decimal column's type.
CLICKHOUSE_DECIMAL_COLUMN_TYPE_PATTERN = r"Decimal\((\d+),\s*(\d+)\)"
#: A label of an Enum column's type, as the SQL literal it is declared with.
CLICKHOUSE_ENUM_LABEL_PATTERN = r"('(?:[^'\\]|\\.)*')\s*=\s*-?\d+"
#: The column types a column changes to unchecked - every text is one of a text type, a Dynamic holds a
#: value of any type (past its max_types, as one of its shared types), and a container's values have no
#: cast a check could read.
CLICKHOUSE_UNCHECKED_CONVERSION_TYPE_PREFIXES = (
    "String",
    "Dynamic",
    "FixedString",
    "LowCardinality(String",
    "Array(",
    "Map(",
    "Tuple(",
    "Nested(",
)
#: A text read as a value of a type - NULL for a text that isn't one.
CLICKHOUSE_CONVERTED_VALUE_TEMPLATE = "accurateCastOrNull({column}, {type})"
#: A text read as a decimal with every fractional digit it has.
CLICKHOUSE_DECIMAL_TEXT_TEMPLATE = "toDecimal256OrNull({column}, 38)"
#: A text a date column doesn't hold as written - ClickHouse reads an impossible date as 1970-01-01
#: and a day past a month's end as one of the next month.
CLICKHOUSE_UNCONVERTED_DATE_TEMPLATE = "{value} IS NULL OR toString({value}) != substring(trimBoth({column}), 1, 10)"
#: A text a moment column doesn't hold as written - an impossible moment is read as 1900-01-01.
CLICKHOUSE_UNCONVERTED_MOMENT_TEMPLATE = (
    "{value} IS NULL OR NOT startsWith(formatDateTime({value}, '%F %T'), "
    "substring(replaceOne(trimBoth({column}), 'T', ' '), 1, 19))"
)
#: A text a JSON column doesn't hold - one that isn't JSON, or isn't a JSON object.
CLICKHOUSE_UNCONVERTED_JSON_TEMPLATE = "NOT isValidJSON({column}) OR NOT startsWith(trimLeft({column}), '{{')"
#: One stored value of a column a type change doesn't convert.
CLICKHOUSE_UNCONVERTED_VALUE_QUERY = (
    "SELECT {column} AS value FROM {table} WHERE {column} IS NOT NULL AND ({predicate}) LIMIT 1"
)
#: A stored value a column's type change doesn't convert to its new type.
CLICKHOUSE_VALUE_NOT_CONVERTED_MESSAGE = 'column {column} of table {table} holds "{value}", which is no {new_type}'
#: A column's values the server converts to no value of the new type at all.
CLICKHOUSE_TYPE_NOT_CONVERTED_MESSAGE = (
    "column {column} of table {table} is a {old_type}, which ClickHouse converts to no {new_type}: {error}"
)
#: The wrappers of a column type around the type of its values.
CLICKHOUSE_TYPE_WRAPPER_PATTERN = r"(?:Nullable|LowCardinality)\((.*)\)"
#: A number column's type - a change between two of them is checked by the narrowing check.
CLICKHOUSE_NUMBER_COLUMN_TYPE_PATTERN = r"(?:U?Int\d+|Float\d+|Decimal(?:\d+)?\(.*\)|Bool)"
#: A column's comment changed - an empty one for none.
CLICKHOUSE_COLUMN_COMMENT_CHANGE_TEMPLATE = "ALTER TABLE {table} COMMENT COLUMN {column} {comment}"
#: A table's comment changed - an empty one for none.
CLICKHOUSE_TABLE_COMMENT_CHANGE_TEMPLATE = "ALTER TABLE {table} MODIFY COMMENT {comment}"
#: What the shadow database of drift is named after - a random part follows.
CLICKHOUSE_SHADOW_DATABASE_PREFIX = "hare_shadow_"
#: The connection's database with its engine - a shadow of a Replicated one is Replicated too.
CLICKHOUSE_SHADOW_DATABASE_ENGINE_SQL = (
    "SELECT name, engine, engine_full FROM system.databases WHERE name = currentDatabase()"
)
#: Another database of the server a model is in, with its engine - its shadow takes the same engine.
CLICKHOUSE_SHADOW_OTHER_DATABASE_ENGINE_SQL = "SELECT name, engine, engine_full FROM system.databases WHERE name = $1"
#: The shadow database created - with an engine of its own, or the server's default one.
CLICKHOUSE_SHADOW_DATABASE_CREATE_TEMPLATE = "CREATE DATABASE {database}{engine}"
#: The shadow database dropped - its replicated tables' Keeper paths removed before it returns.
CLICKHOUSE_SHADOW_DATABASE_DROP_TEMPLATE = "DROP DATABASE IF EXISTS {database} SYNC"
#: The orders of a projection's ORDER BY - a sorting key, ascending with NULL last.
CLICKHOUSE_PROJECTION_ORDERS = frozenset({None, Order.ASC, Order.ASC_NULLS_LAST})
