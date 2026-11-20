from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.schema_objects.enum_type import EnumType
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.schema_objects.enum_types import EnumTypes
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_ENUM_ARRAY_LABELS_HELD_SQL,
    POSTGRESQL_ENUM_LABELS_HELD_SQL,
    POSTGRESQL_ENUM_TYPE_COLUMNS_SQL,
    POSTGRESQL_SAFE_CREATE_TYPE_BODY,
)
from hare.dialects.postgresql.schema.schema_objects.postgresql_enum_replacement import PostgresqlEnumReplacement
from hare.migrations.exceptions import FieldNarrowingDataLossError

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

    def get_enum_type_create_sql(
        self, enum_type: EnumType, safe: bool = False, *, shared_with: Sequence[EnumType] = ()
    ) -> str:
        labels_sql = self.get_enum_labels_sql(enum_type)
        create_sql = f"CREATE TYPE {self.get_type_sql(enum_type)} AS ENUM ({labels_sql});"
        shared = self.get_shared_schemas(enum_type, shared_with)
        body = create_sql
        if safe:
            body = POSTGRESQL_SAFE_CREATE_TYPE_BODY.format(create_sql=create_sql.removesuffix(";"))
        if shared:
            literal = self.editor.client.dialect.literals.get_string_literal_sql
            message = literal(
                f"Declarations of ENUM type {enum_type.name!r} in the same schema require the same labels"
            )
            conflict_sql = f"RAISE EXCEPTION USING MESSAGE = {message};"
            labels = f"ARRAY[{labels_sql}]::text[]"
            cases = {}
            for schema, other in shared.items():
                if other.labels != enum_type.labels:
                    cases[schema] = conflict_sql
                    continue
                type_sql = self.get_type_sql(EnumType(enum_type.name, enum_type.labels, schema))
                identity = f"pg_catalog.to_regtype({literal(type_sql)})"
                ensure_type = f"IF {identity} IS NULL THEN {body}"
                if safe:
                    cases[schema] = f"{ensure_type} END IF;"
                    continue
                # The identity is built from a quoted type name passed as a SQL string literal.
                actual_labels = (
                    "(SELECT ARRAY(SELECT enumlabel::text FROM pg_catalog.pg_enum "  # nosec B608
                    "WHERE enumtypid = t.oid ORDER BY enumsortorder) "
                    f"FROM pg_catalog.pg_type t WHERE t.oid = {identity} AND t.typtype = 'e')"
                )
                cases[schema] = (
                    f"{ensure_type} ELSIF {actual_labels} IS DISTINCT FROM {labels} THEN {conflict_sql} END IF;"
                )
            return self.get_shared_type_sql(body, cases)
        return self.editor.get_anonymous_block_sql(body) + ";" if safe else create_sql

    @staticmethod
    def get_shared_schemas(enum_type: EnumType, shared_with: Sequence[EnumType]) -> dict[str, EnumType]:
        """The explicit schemas which coincide with the unqualified declaration on one search path."""
        if enum_type.schema is None:
            return {
                other.schema: other
                for other in shared_with
                if other.name == enum_type.name and other.schema is not None
            }
        unqualified = next(
            (other for other in shared_with if other.name == enum_type.name and other.schema is None), None
        )
        return {enum_type.schema: unqualified} if unqualified is not None else {}

    def get_shared_type_sql(self, statement: str, cases: dict[str, str]) -> str:
        """Keep offline SQL executable with the server's actual current schema."""
        if not cases:
            return statement
        literal = self.editor.client.dialect.literals.get_string_literal_sql
        branches = " ".join(f"WHEN {literal(schema)} THEN {command}" for schema, command in sorted(cases.items()))
        return (
            self.editor.get_anonymous_block_sql(
                f"BEGIN CASE current_schema() {branches} ELSE {statement} END CASE; END;"
            )
            + ";"
        )

    async def drop_enum_type(self, enum_type: EnumType, *, shared_with: Sequence[EnumType] = ()) -> None:
        retained = {schema: "NULL;" for schema in self.get_shared_schemas(enum_type, shared_with)}
        await self.editor.run_sql(self.get_shared_type_sql(f"DROP TYPE {self.get_type_sql(enum_type)};", retained))

    async def alter_enum_type(
        self, old_type: EnumType, new_type: EnumType, *, shared_with: Sequence[EnumType] = ()
    ) -> None:
        """Adds the new labels in place (``ALTER TYPE ... ADD VALUE``) when the old ones keep their
        order; otherwise replaces it from a locked catalog snapshot of columns and defaults."""
        old_labels = set(old_type.labels)
        kept_in_order = [label for label in new_type.labels if label in old_labels] == list(old_type.labels)
        changed_schemas = frozenset(
            schema
            for schema, other in self.get_shared_schemas(new_type, shared_with).items()
            if other.labels == new_type.labels
        )
        if kept_in_order:
            await self.add_enum_labels(old_type, new_type, changed_schemas)
            return
        await SchemaChangePlan((PostgresqlEnumReplacement(old_type, new_type, changed_schemas),)).execute(self.editor)

    async def check_removed_labels_unused(
        self, old_type: EnumType, new_type: EnumType, *, columns: list[dict[str, Any]] | None = None
    ) -> None:
        """Refuses to drop labels a row still holds - in a column of the type or of an array of it.

        Args:
            old_type: The type as it is.
            new_type: The type as it becomes.
            columns: Previously read catalog rows for columns using the old enum type, if supplied.

        Raises:
            FieldNarrowingDataLossError: A row holds a label ``new_type`` lacks.
        """
        new_labels = set(new_type.labels)
        removed_labels = [label for label in old_type.labels if label not in new_labels]
        if not removed_labels or self.editor.collect_sql:
            return
        client = self.editor.client
        if columns is None:
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

    async def add_enum_labels(
        self, old_type: EnumType, new_type: EnumType, changed_schemas: frozenset[str] = frozenset()
    ) -> None:
        """Adds the labels of ``new_type`` ``old_type`` lacks, each before the next label already in
        the type - or at the end.

        Args:
            old_type: The type as it is.
            new_type: The type as it becomes - the old labels in their order, others among them.
            changed_schemas: Schemas where the shared type already has the new labels.
        """
        literals = self.editor.client.dialect.literals
        present = set(old_type.labels)
        labels = new_type.labels
        statements = []
        type_sql = self.get_type_sql(new_type)
        for index, label in enumerate(labels):
            if label in present:
                continue
            next_label = next((later for later in labels[index + 1 :] if later in present), None)
            position_sql = f" BEFORE {literals.get_string_literal_sql(next_label)}" if next_label is not None else ""
            label_sql = literals.get_string_literal_sql(label)
            ddl = f"ALTER TYPE {type_sql} ADD VALUE"
            suffix = f" {label_sql}{position_sql};"
            statements.append(
                SchemaStatement(
                    self.get_shared_type_sql(
                        f"{ddl}{suffix}", dict.fromkeys(changed_schemas, f"{ddl} IF NOT EXISTS{suffix}")
                    )
                )
            )
            present.add(label)
        await SchemaChangePlan(tuple(statements)).execute(self.editor)
