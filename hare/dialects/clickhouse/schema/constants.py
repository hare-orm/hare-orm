from __future__ import annotations

import re

#: The fields a ClickHouse container type maps to.
CLICKHOUSE_ARRAY_FIELD_PATH = "hare.fields.data.containers.ArrayField"
CLICKHOUSE_MAP_FIELD_PATH = "hare.fields.data.containers.MapField"
CLICKHOUSE_TUPLE_FIELD_PATH = "hare.fields.data.containers.TupleField"
CLICKHOUSE_NESTED_FIELD_PATH = "hare.fields.data.containers.NestedField"
#: The field a type no field maps to is read as - its values as text.
CLICKHOUSE_FALLBACK_FIELD_PATH = "hare.fields.data.text.TextField"
#: The fields of a dictionary-encoded type and of an enum's labels - an enum's members have no class
#: the database names.
CLICKHOUSE_LOW_CARDINALITY_FIELD_PATH = "hare.dialects.clickhouse.fields.LowCardinalityField"
CLICKHOUSE_ENUM_LABEL_FIELD_PATH = "hare.fields.data.text.CharField"
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
#: The rows of a materialized view's query written into it.
CLICKHOUSE_MATERIALIZED_VIEW_FILL_TEMPLATE = "INSERT INTO {view}\n{query};"
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
    "LAYOUT({layout}) LIFETIME(MIN {lifetime_minimum} MAX {lifetime_maximum});"
)
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
#: The table options ClickHouse changes with an ``ALTER TABLE`` - a change of any other one remakes the
#: table.
CLICKHOUSE_ALTERABLE_TABLE_OPTIONS = frozenset(
    {"ttl", "settings", "column_codecs", "column_ttls", "projections", "lightweight_updates"}
)
#: The table settings set when the table is created and never after.
CLICKHOUSE_CREATION_ONLY_TABLE_SETTINGS = frozenset({"index_granularity", "index_granularity_bytes"})
