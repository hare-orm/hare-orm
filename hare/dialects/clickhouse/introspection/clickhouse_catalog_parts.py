from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_INDEX_TYPE_ARGUMENT_DEFAULTS,
    CLICKHOUSE_INDEX_TYPE_ARGUMENTS,
    CLICKHOUSE_OBJECT_PART_TYPES,
)
from hare.dialects.clickhouse.introspection.clickhouse_observed_table_options import ClickhouseObservedTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import CLICKHOUSE_ENGINE_CLAUSES
from hare.inspectdb.introspection.enums import SchemaPartType
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.inspectdb.introspection.table_parts import TableParts

if TYPE_CHECKING:
    from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
    from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot
    from hare.inspectdb.introspection.table_info import TableInfo


class ClickhouseCatalogParts:
    """Keep physical column and skipping-index properties that fields cannot express."""

    @staticmethod
    def get_objects(snapshot: CatalogSnapshot) -> list[SchemaPart]:
        """Compare requested schema objects independently of declaration reconstruction."""
        records = snapshot.copy_records()
        requested = records["requested"][0]["names"]
        parts = []
        for row in records["objects"]:
            part_type = CLICKHOUSE_OBJECT_PART_TYPES[row["engine"]]
            if row["name"] in requested.get(part_type, ()):
                parts.append(SchemaPart(part_type, "", row["name"], {"query": row["create_table_query"]}))
        return parts

    @staticmethod
    def get(snapshot: CatalogSnapshot, tables: list[TableInfo]) -> list[SchemaPart]:
        definitions: dict[tuple[SchemaPartType, str, str], Any] = {}
        table_infos = {table.name: table for table in tables}
        for raw in snapshot.copy_records().get("tables", []):
            table = raw["table_name"]
            for row in raw["columns"]:
                definitions[SchemaPartType.COLUMN, table, row["name"]] = {
                    key: value for key, value in row.items() if key != "is_in_primary_key"
                }
            for row in raw["indexes"]:
                definitions[SchemaPartType.INDEX, table, row["name"]] = ClickhouseCatalogParts.get_index_definition(
                    row
                )
            definitions[SchemaPartType.PRIMARY_KEY, table, table] = {
                "sql": raw["table_rows"][0]["primary_key"],
            }
            clauses = ClickhouseSqlParts.get_clauses(raw["table_rows"][0]["engine_full"], CLICKHOUSE_ENGINE_CLAUSES)
            options = cast("ClickhouseTableOptions | None", table_infos[table].table_options)
            definitions[SchemaPartType.TABLE_OPTIONS, table, table] = {
                "engine": clauses.pop(""),
                "clauses": {
                    name: ClickhouseObservedTableOptions.get_settings(value) if name == "SETTINGS" else {"sql": value}
                    for name, value in clauses.items()
                },
                "column_ttls": SchemaPart.get_definition(options.column_ttls) if options is not None else [],
                "projections": SchemaPart.get_definition(options.projections) if options is not None else [],
            }
        return TableParts.from_catalog(tables, definitions)

    @staticmethod
    def get_index_definition(row: dict[str, Any]) -> dict[str, Any]:
        """Compare omitted native index arguments with their explicit defaults.

        Keep the immutable snapshot's original text. Normalize the catalog's optional
        outer expression parentheses and expand recognized argument-free index defaults.
        """
        definition = dict(row)
        definition["expr"] = ClickhouseSqlParts.get_unwrapped(row["expr"])
        defaults = CLICKHOUSE_INDEX_TYPE_ARGUMENT_DEFAULTS.get(row["type"])
        if defaults and row["type_full"] == row["type"]:
            arguments = ", ".join(defaults[name] for name in CLICKHOUSE_INDEX_TYPE_ARGUMENTS[row["type"]])
            definition["type_full"] = f"{row['type']}({arguments})"
        return definition
