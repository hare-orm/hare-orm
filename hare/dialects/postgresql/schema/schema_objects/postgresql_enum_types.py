from __future__ import annotations

import re
from typing import TYPE_CHECKING

from hare.ddl.schema_objects.enum_type import EnumType
from hare.dialects.base.schema.schema_objects.enum_types import EnumTypes
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_ENUM_ARRAY_LABELS_HELD_SQL,
    POSTGRESQL_ENUM_LABELS_HELD_SQL,
    POSTGRESQL_ENUM_TYPE_COLUMNS_SQL,
    POSTGRESQL_REPLACE_ENUM_TYPE_SQL,
    POSTGRESQL_REPLACED_ENUM_TYPE_SUFFIX,
    POSTGRESQL_SAFE_CREATE_TYPE_SQL,
)
from hare.migrations.exceptions import FieldNarrowingDataLossError
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlEnumTypes(EnumTypes):
    """EnumTypes as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_type_sql(self, enum_type: EnumType, name: str | None = None) -> str:
        """The type's name in SQL - with its schema when it has one.

        Args:
            enum_type: The type.
            name: Another name in the type's schema - None for the type's own.

        Returns:
            The quoted name.
        """
        name_sql = self.editor.quote(name if name is not None else enum_type.name)
        return name_sql if enum_type.schema is None else f"{self.editor.quote(enum_type.schema)}.{name_sql}"

    def get_enum_type_create_sql(self, enum_type: EnumType, safe: bool = False) -> str:
        create_sql = f"CREATE TYPE {self.get_type_sql(enum_type)} AS ENUM ({self.get_enum_labels_sql(enum_type)})"
        return POSTGRESQL_SAFE_CREATE_TYPE_SQL.format(create_sql=create_sql) if safe else f"{create_sql};"

    async def drop_enum_type(self, enum_type: EnumType) -> None:
        await self.editor.run_sql(f"DROP TYPE {self.get_type_sql(enum_type)};")

    async def alter_enum_type(self, old_type: EnumType, new_type: EnumType) -> None:
        """Adds the new labels in place (``ALTER TYPE ... ADD VALUE``) when the old ones keep their
        order; otherwise replaces the type, converting its columns (``POSTGRESQL_REPLACE_ENUM_TYPE_SQL``)."""
        old_labels = set(old_type.labels)
        kept_in_order = [label for label in new_type.labels if label in old_labels] == list(old_type.labels)
        if kept_in_order:
            await self.add_enum_labels(old_type, new_type)
            return
        await self.check_removed_labels_unused(old_type, new_type)
        literals = self.editor.client.dialect.literals
        old_name = Identifiers.get_within_limit(f"{new_type.name}{POSTGRESQL_REPLACED_ENUM_TYPE_SUFFIX}")
        # A default names the old type with its schema when the search path doesn't find it.
        schema_pattern = "" if new_type.schema is None else f'(?:"?{re.escape(new_type.schema)}"?\\.)?'
        await self.editor.run_sql(
            POSTGRESQL_REPLACE_ENUM_TYPE_SQL.format(
                type_sql=self.get_type_sql(new_type),
                old_type_name_sql=self.editor.quote(old_name),
                old_type_sql=self.get_type_sql(new_type, old_name),
                labels_sql=self.get_enum_labels_sql(new_type),
                type_literal=literals.get_string_literal_sql(self.get_type_sql(new_type)),
                old_type_literal=literals.get_string_literal_sql(self.get_type_sql(new_type, old_name)),
                old_type_pattern_literal=literals.get_string_literal_sql(
                    f'::{schema_pattern}"?{re.escape(old_name)}"?'
                ),
            )
        )

    async def check_removed_labels_unused(self, old_type: EnumType, new_type: EnumType) -> None:
        """Refuses to drop labels a row still holds - in a column of the type or of an array of it.

        Args:
            old_type: The type as it is.
            new_type: The type as it becomes.

        Raises:
            FieldNarrowingDataLossError: A row holds a label ``new_type`` lacks.
        """
        new_labels = set(new_type.labels)
        removed_labels = [label for label in old_type.labels if label not in new_labels]
        if not removed_labels or self.editor.collect_sql:
            return
        client = self.editor.client
        columns = await client.execute_dicts(POSTGRESQL_ENUM_TYPE_COLUMNS_SQL, [self.get_type_sql(old_type)])
        for column in columns:
            table_sql = f"{self.editor.quote(column['schema_name'])}.{self.editor.quote(column['table_name'])}"
            column_sql = self.editor.quote(column["column_name"])
            held_sql = POSTGRESQL_ENUM_ARRAY_LABELS_HELD_SQL if column["is_array"] else POSTGRESQL_ENUM_LABELS_HELD_SQL
            (row,) = await client.execute_dicts(held_sql.format(table=table_sql, column=column_sql), [removed_labels])
            if row["held_count"]:
                raise FieldNarrowingDataLossError(
                    f"Cannot remove label(s) {', '.join(map(repr, removed_labels))} from ENUM type "
                    f"{self.get_type_sql(old_type)} - {row['held_count']} row(s) of {table_sql} hold them in "
                    f"{column_sql}. Change those values by hand first, then retry the migration."
                )

    def get_enum_labels_sql(self, enum_type: EnumType) -> str:
        """The labels of an ``ENUM`` type as string literals, comma-separated."""
        literals = self.editor.client.dialect.literals
        return ", ".join(literals.get_string_literal_sql(label) for label in enum_type.labels)

    async def add_enum_labels(self, old_type: EnumType, new_type: EnumType) -> None:
        """Adds the labels of ``new_type`` ``old_type`` lacks, each before the next label already in
        the type - or at the end.

        Args:
            old_type: The type as it is.
            new_type: The type as it becomes - the old labels in their order, others among them.
        """
        literals = self.editor.client.dialect.literals
        present = set(old_type.labels)
        labels = new_type.labels
        for index, label in enumerate(labels):
            if label in present:
                continue
            next_label = next((later for later in labels[index + 1 :] if later in present), None)
            position_sql = f" BEFORE {literals.get_string_literal_sql(next_label)}" if next_label is not None else ""
            await self.editor.run_sql(
                f"ALTER TYPE {self.get_type_sql(new_type)} ADD VALUE {literals.get_string_literal_sql(label)}"
                f"{position_sql};"
            )
            present.add(label)
