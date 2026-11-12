from __future__ import annotations

import dataclasses
from typing import Any

from hare.inspectdb.constants import CATALOG_DIAGNOSTIC_PART_TYPES
from hare.inspectdb.enums import CatalogDiagnosticCode, SchemaPartType
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.inspectdb.introspection.table_info import TableInfo


class TableParts:
    """The parts of a table read back - what an introspector's ``TableInfo`` holds, part by part."""

    @classmethod
    def from_catalog(
        cls, tables: list[TableInfo], definitions: dict[tuple[SchemaPartType, str, str], Any]
    ) -> list[SchemaPart]:
        """Keep declaration values for suggested operations, compare physical definitions.

        A physical object omitted by reconstruction is still included. Its raw
        definition participates in comparison even when no declaration value exists.
        """
        parts = {part.key: part for table in tables for part in cls.get(table)}
        for key, definition in definitions.items():
            previous = parts.get(key)
            parts[key] = (
                dataclasses.replace(previous, definition=definition)
                if previous is not None
                else SchemaPart(*key, definition)
            )
        return list(parts.values())

    @classmethod
    def get(cls, table_info: TableInfo) -> list[SchemaPart]:
        """Every part of a table.

        Args:
            table_info: The table as the introspector read it.

        Returns:
            The parts: the table itself, its primary key, columns, indexes, constraints, foreign keys,
            triggers, policies, grants, sequences, options, row level security and comment.
        """
        table = table_info.name
        parts = [SchemaPart(SchemaPartType.TABLE, table, table, None, table_info)]
        primary_key_columns = sorted(
            (column for column in table_info.columns if column.is_pk), key=lambda column: column.pk_position or 0
        )
        parts.append(
            SchemaPart.of(
                SchemaPartType.PRIMARY_KEY,
                table,
                table,
                {
                    "columns": [column.name for column in primary_key_columns],
                    "without_overlaps": table_info.primary_key_without_overlaps,
                },
            )
        )
        for column in table_info.columns:
            # The primary key, the column's unique key and index are parts of their own, its enum type's
            # labels the type's part.
            column_without_key = dataclasses.replace(
                column, is_pk=False, pk_position=None, is_unique=False, has_index=False, enum_labels=None
            )
            parts.append(
                SchemaPart(
                    SchemaPartType.COLUMN, table, column.name, SchemaPart.get_definition(column_without_key), column
                )
            )
        for index in (*table_info.indexes, *table_info.column_indexes):
            parts.append(SchemaPart.of(SchemaPartType.INDEX, table, index.name, index))
        for check_constraint in table_info.check_constraints:
            parts.append(SchemaPart.of(SchemaPartType.CONSTRAINT, table, check_constraint.name, check_constraint))
        for exclusion_constraint in table_info.exclusion_constraints:
            parts.append(
                SchemaPart.of(SchemaPartType.CONSTRAINT, table, exclusion_constraint.name, exclusion_constraint)
            )
        for column_name, foreign_key in table_info.foreign_keys.items():
            # The target column is read as it is; to_field is inferred from the target table, which one
            # database may lack.
            foreign_key_part = dataclasses.replace(foreign_key, to_field=None)
            parts.append(
                SchemaPart(
                    SchemaPartType.FOREIGN_KEY,
                    table,
                    column_name,
                    SchemaPart.get_definition(foreign_key_part),
                    foreign_key,
                )
            )
        for composite_foreign_key in table_info.composite_foreign_keys:
            parts.append(
                SchemaPart.of(
                    SchemaPartType.FOREIGN_KEY, table, ",".join(composite_foreign_key.columns), composite_foreign_key
                )
            )
        for trigger in table_info.triggers:
            parts.append(SchemaPart.of(SchemaPartType.TRIGGER, table, trigger.name, trigger))
        for policy in table_info.policies:
            parts.append(SchemaPart.of(SchemaPartType.POLICY, table, policy.name, policy))
        for grant in table_info.grants:
            # A grant has no name of its own - it is what it grants.
            definition = SchemaPart.get_definition(grant)
            parts.append(SchemaPart(SchemaPartType.GRANT, table, repr(definition), definition, grant))
        for diagnostic in table_info.diagnostics:
            if diagnostic.code is CatalogDiagnosticCode.PARTITIONING:
                continue
            part_type = SchemaPartType(CATALOG_DIAGNOSTIC_PART_TYPES[diagnostic.code])
            name = diagnostic.name
            definition = diagnostic.definition
            if diagnostic.code is CatalogDiagnosticCode.FOREIGN_KEY:
                name, definition = ",".join(diagnostic.columns), diagnostic.name
            elif diagnostic.code is CatalogDiagnosticCode.GRANT:
                name = definition
            parts.append(SchemaPart.of(part_type, table, name, definition))
        for sequence in table_info.sequences:
            parts.append(SchemaPart.of(SchemaPartType.SEQUENCE, table, sequence.name, sequence))
        parts.append(SchemaPart.of(SchemaPartType.TABLE_OPTIONS, table, table, table_info.table_options))
        parts.append(
            SchemaPart.of(
                SchemaPartType.UNPARSED_PARTITIONING,
                table,
                table,
                next((item.definition for item in table_info.get_diagnostics(CatalogDiagnosticCode.PARTITIONING)), ""),
            )
        )
        parts.append(SchemaPart.of(SchemaPartType.ROW_LEVEL_SECURITY, table, table, table_info.row_level_security))
        parts.append(SchemaPart.of(SchemaPartType.COMMENT, table, table, table_info.table_description or ""))
        return parts
