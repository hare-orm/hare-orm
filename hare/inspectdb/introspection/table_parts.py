from __future__ import annotations

import dataclasses

from hare.inspectdb.introspection.enums import SchemaPartType
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.inspectdb.introspection.table_info import TableInfo


class TableParts:
    """The parts of a table read back - what an introspector's ``TableInfo`` holds, part by part."""

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
        for name, definition in table_info.unparsed_indexes:
            parts.append(SchemaPart.of(SchemaPartType.INDEX, table, name, definition))
        for check_constraint in table_info.check_constraints:
            parts.append(SchemaPart.of(SchemaPartType.CONSTRAINT, table, check_constraint.name, check_constraint))
        for exclusion_constraint in table_info.exclusion_constraints:
            parts.append(
                SchemaPart.of(SchemaPartType.CONSTRAINT, table, exclusion_constraint.name, exclusion_constraint)
            )
        for name, definition in (*table_info.unparsed_check_constraints, *table_info.unparsed_exclusion_constraints):
            parts.append(SchemaPart.of(SchemaPartType.CONSTRAINT, table, name, definition))
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
        for name, columns in table_info.unparsed_foreign_keys:
            parts.append(SchemaPart.of(SchemaPartType.FOREIGN_KEY, table, ",".join(columns), name))
        for trigger in table_info.triggers:
            parts.append(SchemaPart.of(SchemaPartType.TRIGGER, table, trigger.name, trigger))
        for name, definition in table_info.unparsed_triggers:
            parts.append(SchemaPart.of(SchemaPartType.TRIGGER, table, name, definition))
        for policy in table_info.policies:
            parts.append(SchemaPart.of(SchemaPartType.POLICY, table, policy.name, policy))
        for grant in table_info.grants:
            # A grant has no name of its own - it is what it grants.
            definition = SchemaPart.get_definition(grant)
            parts.append(SchemaPart(SchemaPartType.GRANT, table, repr(definition), definition, grant))
        for unparsed_grant in table_info.unparsed_grants:
            parts.append(SchemaPart.of(SchemaPartType.GRANT, table, unparsed_grant, unparsed_grant))
        for sequence in table_info.sequences:
            parts.append(SchemaPart.of(SchemaPartType.SEQUENCE, table, sequence.name, sequence))
        parts.append(SchemaPart.of(SchemaPartType.TABLE_OPTIONS, table, table, table_info.table_options))
        parts.append(SchemaPart.of(SchemaPartType.ROW_LEVEL_SECURITY, table, table, table_info.row_level_security))
        parts.append(SchemaPart.of(SchemaPartType.COMMENT, table, table, table_info.table_description or ""))
        return parts
