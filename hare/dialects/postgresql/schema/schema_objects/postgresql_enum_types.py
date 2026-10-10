from __future__ import annotations

import re
from typing import TYPE_CHECKING

from hare.ddl.schema_objects.enum_type import EnumType
from hare.dialects.base.schema.schema_objects.enum_types import EnumTypes
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_REPLACE_ENUM_TYPE_SQL,
    POSTGRESQL_REPLACED_ENUM_TYPE_SUFFIX,
    POSTGRESQL_SAFE_CREATE_TYPE_SQL,
)
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlEnumTypes(EnumTypes):
    """EnumTypes as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_enum_type_create_sql(self, enum_type: EnumType, safe: bool = False) -> str:
        create_sql = f"CREATE TYPE {self.editor.quote(enum_type.name)} AS ENUM ({self.get_enum_labels_sql(enum_type)})"
        return POSTGRESQL_SAFE_CREATE_TYPE_SQL.format(create_sql=create_sql) if safe else f"{create_sql};"

    async def drop_enum_type(self, enum_type: EnumType) -> None:
        await self.editor.run_sql(f"DROP TYPE {self.editor.quote(enum_type.name)};")

    async def alter_enum_type(self, old_type: EnumType, new_type: EnumType) -> None:
        """Adds the new labels in place (``ALTER TYPE ... ADD VALUE``) when the old ones keep their
        order; otherwise replaces the type, converting its columns (``POSTGRESQL_REPLACE_ENUM_TYPE_SQL``)."""
        old_labels = set(old_type.labels)
        kept_in_order = [label for label in new_type.labels if label in old_labels] == list(old_type.labels)
        if kept_in_order:
            await self.add_enum_labels(old_type, new_type)
            return
        literals = self.editor.client.dialect.literals
        old_name = Identifiers.get_within_limit(f"{new_type.name}{POSTGRESQL_REPLACED_ENUM_TYPE_SUFFIX}")
        await self.editor.run_sql(
            POSTGRESQL_REPLACE_ENUM_TYPE_SQL.format(
                type_sql=self.editor.quote(new_type.name),
                old_type_sql=self.editor.quote(old_name),
                labels_sql=self.get_enum_labels_sql(new_type),
                type_literal=literals.get_string_literal_sql(self.editor.quote(new_type.name)),
                old_type_literal=literals.get_string_literal_sql(self.editor.quote(old_name)),
                old_type_pattern_literal=literals.get_string_literal_sql(f'::"?{re.escape(old_name)}"?'),
            )
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
                f"ALTER TYPE {self.editor.quote(new_type.name)} ADD VALUE {literals.get_string_literal_sql(label)}"
                f"{position_sql};"
            )
            present.add(label)
