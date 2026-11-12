from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING

from hare.dialects.postgresql.constants import (
    POSTGRESQL_FUNCTIONS_SQL,
    POSTGRESQL_INSPECTED_FUNCTION_PRIVILEGES_SQL,
    POSTGRESQL_INSPECTED_SEQUENCE_PRIVILEGES_SQL,
    POSTGRESQL_SCHEMA_EXISTS_SQL,
    POSTGRESQL_TABLES_SQL,
    POSTGRESQL_UNIQUE_INDEX_COLUMNS_SQL,
    POSTGRESQL_VIEW_DEFINITIONS_SQL,
)
from hare.dialects.postgresql.introspection.constants import POSTGRESQL_TABLE_CATALOG_QUERIES
from hare.fields.data.json.json_codec import JsonCodec
from hare.inspectdb.enums import SchemaPartType
from hare.inspectdb.exceptions import SchemaNotFoundError, TableNotFoundError
from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class PostgresqlCatalogReader:
    """Read physical table metadata in batches without inferring fields or models."""

    @staticmethod
    async def read_objects(
        connection: DatabaseClient, schema: str, object_names: Mapping[SchemaPartType, Collection[str]]
    ) -> CatalogSnapshot:
        """Capture function overloads, views and privileges before interpreting declarations."""
        view_names = sorted(
            set((*object_names.get(SchemaPartType.VIEW, ()), *object_names.get(SchemaPartType.MATERIALIZED_VIEW, ())))
        )
        views = (
            await connection.execute_dicts(POSTGRESQL_VIEW_DEFINITIONS_SQL, [schema, view_names]) if view_names else []
        )
        for row in views:
            row["unique_indexes"] = (
                await connection.execute_dicts(POSTGRESQL_UNIQUE_INDEX_COLUMNS_SQL, [schema, row["name"]])
                if row["is_materialized"]
                else []
            )
        function_names = sorted(set(object_names.get(SchemaPartType.FUNCTION, ())))
        functions = (
            await connection.execute_dicts(POSTGRESQL_FUNCTIONS_SQL, [schema, function_names])
            if function_names
            else []
        )
        return CatalogSnapshot(
            schema,
            (),
            {
                "requested": [{"names": {part_type: list(names) for part_type, names in object_names.items()}}],
                "views": views,
                "functions": functions,
                "sequence_privileges": await connection.execute_dicts(
                    POSTGRESQL_INSPECTED_SEQUENCE_PRIVILEGES_SQL, [schema]
                ),
                "function_privileges": await connection.execute_dicts(
                    POSTGRESQL_INSPECTED_FUNCTION_PRIVILEGES_SQL, [schema]
                ),
            },
        )

    @staticmethod
    async def read_tables(connection: DatabaseClient, tables: list[str], schema: str) -> CatalogSnapshot:
        """Retain all catalog records with one query per section, independent of table count.

        The supplied connection controls transaction isolation. No model declarations,
        defaults, index expressions or function bodies are evaluated during this read.
        A missing requested table is reported before the remaining catalog queries.
        """
        if not tables:
            return CatalogSnapshot(schema, (), {})
        requested = list(dict.fromkeys(tables))
        table_rows = await connection.execute_dicts(POSTGRESQL_TABLES_SQL, [schema, requested])
        if not table_rows and not await connection.execute_dicts(POSTGRESQL_SCHEMA_EXISTS_SQL, [schema]):
            raise SchemaNotFoundError(schema)
        existing = {row["table_name"] for row in table_rows}
        for table in requested:
            if table not in existing:
                raise TableNotFoundError(table)
        records = {"tables": table_rows}
        for section, query in POSTGRESQL_TABLE_CATALOG_QUERIES.items():
            records[section] = await connection.execute_dicts(query, [schema, requested])
        for column in records["columns"]:
            sequences = column.get("column_sequences")
            if isinstance(sequences, (str, bytes)):
                column["column_sequences"] = JsonCodec.loads(sequences)
        return CatalogSnapshot(schema, tuple(tables), records)
