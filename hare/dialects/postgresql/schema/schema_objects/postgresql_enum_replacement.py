from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast

from hare.ddl.schema_objects.enum_type import EnumType
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_ENUM_DEPENDENCIES_CHANGED,
    POSTGRESQL_ENUM_DEPENDENCIES_UNSUPPORTED,
    POSTGRESQL_ENUM_GRANTS_UNSUPPORTED,
    POSTGRESQL_ENUM_IDENTITY_CHANGED,
    POSTGRESQL_ENUM_REPLACEMENT_PREVIEW,
    POSTGRESQL_ENUM_TYPE_COLUMNS_SQL,
    POSTGRESQL_ENUM_TYPE_GRANTS_SQL,
    POSTGRESQL_ENUM_TYPE_METADATA_SQL,
    POSTGRESQL_ENUM_UNSUPPORTED_DEPENDENCIES_SQL,
    POSTGRESQL_REPLACED_ENUM_TYPE_SUFFIX,
)
from hare.exceptions import UnSupportedError
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


@dataclass(frozen=True, slots=True)
class PostgresqlEnumReplacement:
    """Replace an enum from a catalog snapshot taken before renaming its identity.

    All affected tables are locked before checking stored labels or reading defaults.
    The complete operation uses one transaction/savepoint, including when the caller
    invokes the schema editor directly. Defaults are restored as original catalog
    expressions: neither casts nor string literals are rewritten. Dependencies that
    cannot be recreated from this snapshot require explicit migration operations.
    """

    old: EnumType
    new: EnumType
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset(
        {SchemaEffect.READ_CATALOG, SchemaEffect.LOCK_TABLES, SchemaEffect.CHECK_ROWS, SchemaEffect.REWRITE_ROWS}
    )

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Collect an honest runtime step, or take locks and execute its catalog-derived SQL."""
        schema_editor = cast("PostgresqlSchemaEditor", editor)
        if schema_editor.collect_sql:
            schema_editor.collected_sql.append(
                POSTGRESQL_ENUM_REPLACEMENT_PREVIEW.format(type=schema_editor.enum_types.get_type_sql(self.old))
            )
            return
        async with schema_editor.client._in_transaction() as connection:
            transaction_editor = type(schema_editor)(connection, atomic=schema_editor.atomic)
            try:
                await self._replace(transaction_editor)
            finally:
                schema_editor.run_statement_count += transaction_editor.run_statement_count

    async def _replace(self, editor: PostgresqlSchemaEditor) -> None:
        client = editor.client
        type_sql = editor.enum_types.get_type_sql(self.old)
        (metadata,) = await client.execute_dicts(POSTGRESQL_ENUM_TYPE_METADATA_SQL, [type_sql])
        old = EnumType(metadata["name"], tuple(metadata["labels"]), metadata["schema_name"])
        new = EnumType(old.name, self.new.labels, old.schema)
        type_sql = editor.enum_types.get_type_sql(old)
        columns = await client.execute_dicts(POSTGRESQL_ENUM_TYPE_COLUMNS_SQL, [type_sql])
        tables = sorted({(column["schema_name"], column["table_name"]) for column in columns})
        if tables:
            names = ", ".join(editor.qualify_table_name(table, schema) for schema, table in tables)
            await client.execute(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE", returns_rows=False)
        # A default may have changed while waiting for a table lock. Only this second
        # snapshot is used for restoration, and it precedes every schema mutation.
        columns = await client.execute_dicts(POSTGRESQL_ENUM_TYPE_COLUMNS_SQL, [type_sql])
        if {(column["schema_name"], column["table_name"]) for column in columns} - set(tables):
            raise UnSupportedError(POSTGRESQL_ENUM_DEPENDENCIES_CHANGED.format(type=type_sql))
        (locked_metadata,) = await client.execute_dicts(POSTGRESQL_ENUM_TYPE_METADATA_SQL, [type_sql])
        if locked_metadata["type_oid"] != metadata["type_oid"]:
            raise UnSupportedError(POSTGRESQL_ENUM_IDENTITY_CHANGED.format(type=type_sql))
        metadata = locked_metadata
        old = EnumType(metadata["name"], tuple(metadata["labels"]), metadata["schema_name"])
        unsupported = await client.execute_dicts(POSTGRESQL_ENUM_UNSUPPORTED_DEPENDENCIES_SQL, [type_sql])
        if unsupported:
            names = ", ".join(row["description"] for row in unsupported)
            raise UnSupportedError(POSTGRESQL_ENUM_DEPENDENCIES_UNSUPPORTED.format(type=type_sql, objects=names))
        grants = await client.execute_dicts(POSTGRESQL_ENUM_TYPE_GRANTS_SQL, [type_sql])
        if any(not grant["granted_by_owner"] for grant in grants):
            raise UnSupportedError(POSTGRESQL_ENUM_GRANTS_UNSUPPORTED.format(type=type_sql))
        await editor.enum_types.check_removed_labels_unused(old, new, columns=columns)

        old_name = Identifiers.get_within_limit(f"{new.name}{POSTGRESQL_REPLACED_ENUM_TYPE_SUFFIX}")
        replacement_type_sql = editor.enum_types.get_type_sql(new)
        await editor.run_sql(f"ALTER TYPE {type_sql} RENAME TO {editor.quote(old_name)}")
        await editor.run_sql(editor.enum_types.get_enum_type_create_sql(new))
        # Drop every dependent default before converting any column. A later column's
        # default can otherwise keep the old enum alive during an earlier conversion.
        for column in columns:
            if column["default_sql"] is not None:
                table = editor.qualify_table_name(column["table_name"], column["schema_name"])
                await editor.run_sql(
                    f"ALTER TABLE ONLY {table} ALTER COLUMN {editor.quote(column['column_name'])} DROP DEFAULT"
                )
        for column in columns:
            # Altering the parent changes inherited columns too; their individual
            # defaults are still restored separately below.
            if column["inherited"]:
                continue
            table = editor.qualify_table_name(column["table_name"], column["schema_name"])
            name = editor.quote(column["column_name"])
            suffix = "[]" if column["is_array"] else ""
            await editor.run_sql(
                f"ALTER TABLE {table} ALTER COLUMN {name} TYPE {replacement_type_sql}{suffix} "
                f"USING {name}::text{suffix}::{replacement_type_sql}{suffix}"
            )
        for column in columns:
            if column["default_sql"] is not None:
                table = editor.qualify_table_name(column["table_name"], column["schema_name"])
                await editor.run_sql(
                    f"ALTER TABLE ONLY {table} ALTER COLUMN {editor.quote(column['column_name'])} "
                    f"SET DEFAULT {column['default_sql']}"
                )
        # RESTRICT is intentional: concurrent new dependencies abort the transaction,
        # rather than deleting objects outside the prepared catalog snapshot.
        await editor.run_sql(f"DROP TYPE {editor.enum_types.get_type_sql(old, old_name)}")
        # New CREATE TYPE may inherit different default privileges from the role.
        # Restore the original effective ACL, rather than adding to those defaults.
        initial_grants = await client.execute_dicts(POSTGRESQL_ENUM_TYPE_GRANTS_SQL, [replacement_type_sql])
        if initial_grants:
            roles = ", ".join(
                "PUBLIC" if grant["grantee"] is None else editor.quote(grant["grantee"]) for grant in initial_grants
            )
            await editor.run_sql(f"REVOKE ALL ON TYPE {replacement_type_sql} FROM {roles}")
        for grant in grants:
            role = "PUBLIC" if grant["grantee"] is None else editor.quote(grant["grantee"])
            option = " WITH GRANT OPTION" if grant["is_grantable"] else ""
            await editor.run_sql(f"GRANT USAGE ON TYPE {replacement_type_sql} TO {role}{option}")
        if metadata["comment"] is not None:
            comment = client.dialect.literals.get_string_literal_sql(metadata["comment"])
            await editor.run_sql(f"COMMENT ON TYPE {replacement_type_sql} IS {comment}")
        await editor.run_sql(f"ALTER TYPE {replacement_type_sql} OWNER TO {editor.quote(metadata['owner_name'])}")
