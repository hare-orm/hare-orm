from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING

from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_COLUMNS_SQL,
    CLICKHOUSE_CURRENT_DATABASE_SQL,
    CLICKHOUSE_DATABASE_EXISTS_SQL,
    CLICKHOUSE_INDEXES_SQL,
    CLICKHOUSE_OBJECT_DEFINITIONS_SQL,
    CLICKHOUSE_OBJECT_PART_TYPES,
    CLICKHOUSE_TABLE_SQL,
)
from hare.dialects.clickhouse.introspection.constants import CLICKHOUSE_TABLE_SETTING_DEFAULTS_SQL
from hare.inspectdb.enums import SchemaPartType
from hare.inspectdb.exceptions import SchemaNotFoundError, TableNotFoundError
from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class ClickhouseCatalogReader:
    """Capture system-table rows and server defaults independently of declaration parsing."""

    @staticmethod
    async def read_objects(
        connection: DatabaseClient, schema: str, object_names: Mapping[SchemaPartType, Collection[str]]
    ) -> CatalogSnapshot:
        """Retain original object CREATE text, including unsupported clauses."""
        rows = (
            await connection.execute_dicts(CLICKHOUSE_OBJECT_DEFINITIONS_SQL, [schema])
            if any(object_names.get(part_type) for part_type in CLICKHOUSE_OBJECT_PART_TYPES.values())
            else []
        )
        return CatalogSnapshot(
            schema,
            (),
            {
                "requested": [{"names": {part_type: list(names) for part_type, names in object_names.items()}}],
                "objects": rows,
            },
        )

    @staticmethod
    async def read_tables(connection: DatabaseClient, tables: list[str], schema: str) -> CatalogSnapshot:
        """Read shared settings once and retain original engine and CREATE text unchanged."""
        if not tables:
            return CatalogSnapshot(schema, (), {})
        database_rows = await connection.execute_dicts(CLICKHOUSE_CURRENT_DATABASE_SQL)
        settings = await connection.execute_dicts(CLICKHOUSE_TABLE_SETTING_DEFAULTS_SQL)
        records = []
        for table in dict.fromkeys(tables):
            table_rows = await connection.execute_dicts(CLICKHOUSE_TABLE_SQL, [schema, table])
            if not table_rows:
                if not await connection.execute_dicts(CLICKHOUSE_DATABASE_EXISTS_SQL, [schema]):
                    raise SchemaNotFoundError(schema)
                raise TableNotFoundError(table)
            records.append(
                {
                    "table_name": table,
                    "table_rows": table_rows,
                    "columns": await connection.execute_dicts(CLICKHOUSE_COLUMNS_SQL, [schema, table]),
                    "indexes": await connection.execute_dicts(CLICKHOUSE_INDEXES_SQL, [schema, table]),
                }
            )
        return CatalogSnapshot(
            schema,
            tuple(tables),
            {
                "database": database_rows,
                "settings": settings,
                "tables": records,
            },
        )
