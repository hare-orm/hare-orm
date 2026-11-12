from __future__ import annotations

from collections import defaultdict
from operator import itemgetter
from typing import TYPE_CHECKING, Any

from hare.dialects.sqlite.constants import SQLITE_PRIMARY_KEY_INDEX_ORIGIN, SQLITE_ROW_ID_TYPE
from hare.dialects.sqlite.introspection.sqlite_column_declarations import SqliteColumnDeclarations
from hare.dialects.sqlite.introspection.sqlite_foreign_key_clauses import SqliteForeignKeyClauses
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions
from hare.dialects.sqlite.types.sqlite_physical_type import SqlitePhysicalType
from hare.inspectdb.enums import SchemaPartType
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.table_parts import TableParts

if TYPE_CHECKING:
    from hare.dialects.sqlite.sqlite_introspector import SqliteIntrospector
    from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot
    from hare.inspectdb.introspection.schema_part import SchemaPart
    from hare.inspectdb.introspection.table_info import TableInfo


class SqliteCatalogParts:
    """Compare captured storage properties even when no field can represent them.

    Index auxiliary columns and catalog ordinal numbers are not declarations.
    Key order, collation, expressions and foreign-key actions are compared directly;
    declaration values remain available only for migration suggestions.
    """

    @staticmethod
    def get(
        snapshot: CatalogSnapshot, tables: list[TableInfo], introspector: type[SqliteIntrospector]
    ) -> list[SchemaPart]:
        records = snapshot.copy_records()
        table_infos = {table.name: table for table in tables}
        target_columns = {
            row["table_name"]: row["columns"] for row in [*records.get("tables", []), *records.get("references", [])]
        }
        definitions: dict[tuple[SchemaPartType, str, str], Any] = {}
        for raw in records.get("tables", []):
            table = raw["table_name"]
            info = table_infos[table]
            options: SqliteTableOptions | None = info.table_options  # type: ignore[assignment]
            columns = {column.name: column for column in info.columns}
            column_declarations = SqliteColumnDeclarations.get_columns(
                raw["table_rows"][0]["sql"], {row["name"] for row in raw["columns"]}
            )
            has_primary_index = any(row["origin"] == SQLITE_PRIMARY_KEY_INDEX_ORIGIN for row in raw["indexes"])
            for row in raw["columns"]:
                column = columns.get(row["name"])
                row_id = bool(row["pk"]) and not has_primary_index and row["type"].upper() == SQLITE_ROW_ID_TYPE
                definitions[SchemaPartType.COLUMN, table, row["name"]] = {
                    "affinity": SqlitePhysicalType.from_sql(row["type"]).affinity,
                    "collation": SqliteColumnDeclarations.get_collation(column_declarations.get(row["name"], "")),
                    "strict_any": bool(options is not None and options.strict and row["type"].upper() == "ANY"),
                    "nullable": not row["notnull"] and not row_id,
                    "row_id": row_id,
                    "column_default": SchemaIntrospector.strip_outer_parentheses(row["dflt_value"]),
                    "hidden": row["hidden"],
                    "generated_expression": column.generated_expression
                    if column is not None
                    else raw["table_rows"][0]["sql"]
                    if row["hidden"] in (2, 3)
                    else None,
                    "description": column.description if column is not None else None,
                }
            indexes = {index.catalog_name or index.name: index for index in (*info.indexes, *info.column_indexes)}
            index_sql = {row["name"]: row["sql"] for row in raw["index_definitions"]}
            for row in raw["indexes"]:
                keys = [item for item in raw["index_columns"][row["name"]] if item["key"]]
                definition = {
                    "unique": bool(row["unique"]),
                    "keys": [
                        {"name": item["name"], "desc": item["desc"], "coll": item["coll"].casefold()} for item in keys
                    ],
                }
                if row["origin"] == SQLITE_PRIMARY_KEY_INDEX_ORIGIN:
                    definitions[SchemaPartType.PRIMARY_KEY, table, table] = definition
                    continue
                index = indexes.get(row["name"])
                name = index.name if index is not None else row["name"]
                # Expressions and predicates are available only in the original SQL.
                # The mapper strips the CREATE header without reformatting its terms.
                terms, condition = (
                    introspector._parse_sqlite_index_definition(index_sql[row["name"]])
                    if index_sql.get(row["name"])
                    else (None, None)
                )
                definition.update(
                    name=name,
                    expression_terms=terms if any(item["name"] is None for item in keys) else None,
                    condition_sql=condition,
                )
                if index_sql.get(row["name"]) and terms is None:
                    definition["sql"] = index_sql[row["name"]]
                definitions[SchemaPartType.INDEX, table, name] = definition
            foreign_keys: dict[int, list[dict[str, Any]]] = defaultdict(list)
            deferrals = SqliteForeignKeyClauses.get_deferrals(raw, target_columns)
            for row in raw["foreign_keys"]:
                foreign_keys[row["id"]].append(row)
            by_columns: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for rows in foreign_keys.values():
                rows.sort(key=itemgetter("seq"))
                target = rows[0]["table"]
                primary_columns = sorted(
                    (row for row in target_columns.get(target, []) if row["pk"]), key=itemgetter("pk")
                )
                by_columns[",".join(row["from"] for row in rows)].append(
                    {
                        "target_table": target,
                        "columns": [row["from"] for row in rows],
                        "target_columns": [
                            row["to"]
                            if row["to"] is not None
                            else primary_columns[position]["name"]
                            if position < len(primary_columns)
                            else None
                            for position, row in enumerate(rows)
                        ],
                        "on_delete": rows[0]["on_delete"],
                        "on_update": rows[0]["on_update"],
                        "match": rows[0]["match"],
                        "deferral": deferrals[rows[0]["id"]],
                        "sql": raw["table_rows"][0]["sql"] if deferrals[rows[0]["id"]] is None else None,
                    }
                )
            for name, values in by_columns.items():
                definitions[SchemaPartType.FOREIGN_KEY, table, name] = sorted(values, key=repr)
        return TableParts.from_catalog(tables, definitions)
