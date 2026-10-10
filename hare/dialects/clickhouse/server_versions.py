from __future__ import annotations

#: The oldest ClickHouse server hare runs on - lightweight DELETE and the session time zone setting.
CLICKHOUSE_MINIMUM_SERVER_VERSION = (24, 3)
#: The first server taking keys from a series ClickHouse Keeper keeps - generateSerialID.
CLICKHOUSE_KEY_SERIES_SERVER_VERSION = (25, 1)
#: The first server with the table settings rebuilding a table's projections for a lightweight DELETE
#: and for the merges of an engine dropping rows.
CLICKHOUSE_PROJECTION_REBUILD_SERVER_VERSION = (24, 8)
#: The first server whose materialized views refreshed on a schedule are no longer experimental, and
#: are waited for.
CLICKHOUSE_REFRESHABLE_VIEW_SERVER_VERSION = (24, 10)
#: The first server changing rows by a lightweight UPDATE.
CLICKHOUSE_LIGHTWEIGHT_UPDATE_SERVER_VERSION = (25, 7)
#: The first server running correlated subqueries - in SELECT, WHERE and HAVING; an experimental setting
#: turns them on before 25.8.
CLICKHOUSE_CORRELATED_SUBQUERIES_SERVER_VERSION = (25, 4)
#: The first server whose Variant and Dynamic types are no longer experimental.
CLICKHOUSE_VARIANT_TYPES_SERVER_VERSION = (25, 3)
#: The first server whose JSON type is no longer experimental - a JSONField is stored by it from there.
CLICKHOUSE_JSON_TYPE_SERVER_VERSION = (25, 3)
