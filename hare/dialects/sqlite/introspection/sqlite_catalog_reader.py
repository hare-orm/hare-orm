from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.sqlite.constants import (
    SPATIALITE_GEOMETRY_COLUMNS_TABLE_NAME,
    SPATIALITE_INDEXED_COLUMNS_SQL,
    SQLITE_DATABASE_TABLE_EXISTS_SQL,
    SQLITE_INDEX_SQLS_SQL,
    SQLITE_PRIMARY_KEY_INDEX_ORIGIN,
    SQLITE_TABLE_LIST_SQL,
    SQLITE_TABLE_NAMES_SQL,
    SQLITE_TABLE_SQL_SQL,
    SQLITE_TRIGGER_SQLS_SQL,
    SQLITE_VIRTUAL_TABLES_SQL,
)
from hare.inspectdb.exceptions import TableNotFoundError
from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.sqlite.sqlite_introspector import SqliteIntrospector


class SqliteCatalogReader:
    """Read SQLite pragmas and original CREATE text before reconstructing declarations."""

    @staticmethod
    async def read_tables(
        connection: DatabaseClient, tables: list[str], schema: str, introspector: type[SqliteIntrospector]
    ) -> CatalogSnapshot:
        """Capture requested tables and referenced keys; extension metadata is read once."""
        if not tables:
            return CatalogSnapshot(schema, (), {})
        database = await introspector.get_database(connection, schema)
        table_list = {
            row["name"]: {"without_rowid": bool(row["wr"]), "strict": bool(row["strict"])}
            for row in await connection.execute_dicts(SQLITE_TABLE_LIST_SQL.format(database=database))
        }
        identifier_key = connection.dialect.literals.get_identifier_key
        table_names = (
            list(table_list)
            if table_list
            else [
                row["name"] for row in await connection.execute_dicts(SQLITE_TABLE_NAMES_SQL.format(database=database))
            ]
        )
        names_by_key = {identifier_key(name): name for name in table_names}
        tables = list(dict.fromkeys(names_by_key.get(identifier_key(name), name) for name in tables))
        virtual_tables = await connection.execute_dicts(SQLITE_VIRTUAL_TABLES_SQL.format(database=database))
        _, spatial_metadata = await connection.execute(
            SQLITE_DATABASE_TABLE_EXISTS_SQL.format(database=database), [SPATIALITE_GEOMETRY_COLUMNS_TABLE_NAME]
        )
        records = []
        referenced_tables: set[str] = set()
        for table in tables:
            table_rows = await connection.execute_dicts(SQLITE_TABLE_SQL_SQL.format(database=database), [table])
            if not table_rows:
                raise TableNotFoundError(table)
            columns = await connection.execute_dicts(introspector.get_pragma_sql(database, "table_xinfo", table))
            indexes = await connection.execute_dicts(introspector.get_pragma_sql(database, "index_list", table))
            table_options = table_list.get(table)
            if table_options is None:
                # SQLite before 3.37 ignores table_list. index_info(table) describes the storage
                # key of a WITHOUT ROWID table and returns no rows for an ordinary table.
                without_rowid = any(row["origin"] == SQLITE_PRIMARY_KEY_INDEX_ORIGIN for row in indexes) and bool(
                    await connection.execute_dicts(introspector.get_pragma_sql(database, "index_info", table))
                )
                table_options = {"without_rowid": without_rowid, "strict": False}
            index_columns = {
                row["name"]: await connection.execute_dicts(
                    introspector.get_pragma_sql(database, "index_xinfo", row["name"])
                )
                for row in indexes
            }
            foreign_keys = await connection.execute_dicts(
                introspector.get_pragma_sql(database, "foreign_key_list", table)
            )
            for row in foreign_keys:
                referenced_table = names_by_key.get(identifier_key(row["table"]), row["table"])
                row["table"] = referenced_table
                referenced_tables.add(referenced_table)
            records.append(
                {
                    "table_name": table,
                    "table_rows": table_rows,
                    "table_options": table_options,
                    "columns": columns,
                    "indexes": indexes,
                    "index_columns": index_columns,
                    "index_definitions": await connection.execute_dicts(
                        SQLITE_INDEX_SQLS_SQL.format(database=database), [table]
                    ),
                    "foreign_keys": foreign_keys,
                    "triggers": await connection.execute_dicts(
                        SQLITE_TRIGGER_SQLS_SQL.format(database=database), [table]
                    ),
                    "spatial_columns": await connection.execute_dicts(
                        SPATIALITE_INDEXED_COLUMNS_SQL.format(database=database), [table]
                    )
                    if spatial_metadata
                    else [],
                }
            )
        references = [
            {
                "table_name": table,
                "columns": await connection.execute_dicts(introspector.get_pragma_sql(database, "table_xinfo", table)),
            }
            for table in sorted(referenced_tables.difference(tables))
        ]
        reference_column_names = {
            record["table_name"]: {identifier_key(column["name"]): column["name"] for column in record["columns"]}
            for record in [*records, *references]
            if record["table_name"] in referenced_tables
        }
        for record in records:
            for row in record["foreign_keys"]:
                if row["to"] is not None:
                    names = reference_column_names[row["table"]]
                    row["to"] = names.get(identifier_key(row["to"]), row["to"])
        return CatalogSnapshot(
            schema,
            tuple(tables),
            {
                "tables": records,
                "references": references,
                "virtual_tables": virtual_tables,
            },
        )
