from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from hare.ddl.enums import Privilege
from hare.declarations.declaration_values import DeclarationValues
from hare.dialects.postgresql.introspection.constants import (
    POSTGRESQL_CATALOG_COLUMN_NONCOMPARABLE_MEMBERS,
    POSTGRESQL_CATALOG_FOREIGN_KEY_NONCOMPARABLE_MEMBERS,
)
from hare.dialects.postgresql.introspection.postgresql_definitions import PostgresqlDefinitions
from hare.inspectdb.introspection.enums import SchemaPartType
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.inspectdb.introspection.table_parts import TableParts

if TYPE_CHECKING:
    from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot
    from hare.inspectdb.introspection.column_info import ColumnInfo
    from hare.inspectdb.introspection.table_info import TableInfo


class PostgresqlCatalogParts:
    """Compare physical catalog records independently of reconstructed declarations.

    Declaration values are retained only for suggested migration operations. Foreign
    keys that cannot become a composite field, defaults not expressible by fields,
    and unparsed triggers and constraints still retain their physical definitions.
    """

    @staticmethod
    def get(snapshot: CatalogSnapshot, tables: list[TableInfo]) -> list[SchemaPart]:
        """Attach catalog signatures without evaluating defaults or constructing SQL."""
        rows = snapshot.copy_records()
        definitions: dict[tuple[SchemaPartType, str, str], Any] = {}
        partitions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows.get("partitions", []):
            partition = {name: value for name, value in row.items() if name != "table_name"}
            partition["storage_parameters"] = PostgresqlDefinitions.parse_storage_parameters(row["storage_parameters"])
            partitions[row["table_name"]].append(partition)
        for row in rows.get("tables", []):
            definitions[SchemaPartType.TABLE_OPTIONS, row["table_name"], row["table_name"]] = {
                "unlogged": row["unlogged"],
                "storage_parameters": PostgresqlDefinitions.parse_storage_parameters(row["storage_parameters"]),
                "tablespace": None if row["tablespace"] == row["default_tablespace"] else row["tablespace"],
                "partitions": sorted(
                    partitions[row["table_name"]], key=lambda partition: partition["partition_table"] or ""
                ),
            }
        columns = {(table.name, column.name): column for table in tables for column in table.columns}
        for row in rows.get("columns", []):
            definitions[SchemaPartType.COLUMN, row["table_name"], row["column_name"]] = (
                PostgresqlCatalogParts.get_column_definition(row, columns.get((row["table_name"], row["column_name"])))
            )
        foreign_keys: dict[tuple[SchemaPartType, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows.get("foreign_keys", []):
            foreign_keys[SchemaPartType.FOREIGN_KEY, row["table_name"], ",".join(row["columns"])].append(
                {
                    name: value
                    for name, value in row.items()
                    if name not in POSTGRESQL_CATALOG_FOREIGN_KEY_NONCOMPARABLE_MEMBERS
                }
            )
        definitions.update(
            {key: sorted(values, key=DeclarationValues.get_key) for key, values in foreign_keys.items()}
        )
        for row in rows.get("constraints", []):
            definitions[SchemaPartType.CONSTRAINT, row["table_name"], row["conname"]] = {
                "type": row["constraint_type"],
                # Validation is a separate migration step; the declaration remains the same.
                "sql": row["definition"].removesuffix(" NOT VALID"),
            }
        for row in rows.get("triggers", []):
            definition = {name: value for name, value in row.items() if name not in ("table_name", "name")}
            if row["language"] not in ("sql", "plpgsql"):
                definition["literal_body"] = definition.pop("body")
            definitions[SchemaPartType.TRIGGER, row["table_name"], row["name"]] = definition
        for row in rows.get("indexes", []):
            # Key expressions retain operators, collations and opclasses. The CREATE
            # header adds the index and table identity, already stored separately.
            definition = {
                key: value for key, value in row.items() if key not in ("table_name", "index_name", "index_def")
            }
            definition["name"] = row["index_name"]
            definition["storage_parameters"] = PostgresqlDefinitions.parse_storage_parameters(
                row["storage_parameters"]
            )
            if row["is_primary"]:
                definition.pop("name")
                definitions[SchemaPartType.PRIMARY_KEY, row["table_name"], row["table_name"]] = definition
            else:
                definitions[SchemaPartType.INDEX, row["table_name"], row["index_name"]] = definition
        for row in rows.get("policies", []):
            definition = {name: value for name, value in row.items() if name != "table_name"}
            definition["roles"] = sorted(row["roles"])
            definitions[SchemaPartType.POLICY, row["table_name"], row["name"]] = definition
        for row in rows.get("sequences", []):
            if row["declaring_table"] == row["table_name"]:
                definitions[SchemaPartType.SEQUENCE, row["table_name"], row["name"]] = {
                    key: value for key, value in row.items() if key not in ("table_name", "declaring_table")
                }
        parts = []
        for part in TableParts.from_catalog(tables, definitions):
            if part.part_type is SchemaPartType.GRANT and isinstance(part.value, str):
                continue
            if part.part_type is SchemaPartType.COLUMN and "column_sequences" in part.definition:
                part = replace(
                    part, unrepresented_definition={"column_sequences": part.definition["column_sequences"]}
                )
            parts.append(part)
        known_privileges = {privilege.value for privilege in Privilege}
        for row in (*rows.get("table_privileges", []), *rows.get("column_privileges", [])):
            if str(row["privilege_type"]).lower() in known_privileges:
                continue
            definition = {key: value for key, value in row.items() if key != "table_name"}
            parts.append(SchemaPart(SchemaPartType.GRANT, row["table_name"], repr(definition), definition))
        return parts

    @staticmethod
    def get_column_definition(row: dict[str, Any], column: ColumnInfo | None = None) -> dict[str, Any]:
        """Retain sequence settings, identifying generated-key sequences by their column.

        The catalog snapshot retains physical names. An identity sequence's name
        is selected by the server and can differ in the shadow schema; its column
        dependency establishes the comparison identity. A SERIAL key's owned sequence
        also survives table/column renames under its old name. Only its exact nextval
        default is normalized; explicit defaults and independent sequences retain their
        references. Current sequence values are data, not schema settings.
        """
        definition = {
            name: value for name, value in row.items() if name not in POSTGRESQL_CATALOG_COLUMN_NONCOMPARABLE_MEMBERS
        }
        if "column_sequences" in row:
            owned_sequences = row["column_sequences"]
            serial_key = (
                column is not None
                and column.is_pk
                and column.generates_key
                and column.db_default is None
                and len(owned_sequences) == 1
                and owned_sequences[0].get("is_column_default") is True
            )
            sequences = [
                {
                    name: value
                    for name, value in sequence.items()
                    if name not in ("name", "schema", "is_column_default")
                }
                if sequence["dependency_type"] == "i" or serial_key
                else dict(sequence)
                for sequence in owned_sequences
            ]
            definition["column_sequences"] = sorted(sequences, key=DeclarationValues.get_key)
            if serial_key:
                definition["column_default"] = {"generated_key_sequence": True}
        return definition
