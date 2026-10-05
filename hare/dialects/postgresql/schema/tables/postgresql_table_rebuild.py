from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.ddl.enums import GrantTarget
from hare.dialects.base.schema.tables.table_rebuild import TableRebuild
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_SEQUENCE_NO_OWNER_SQL,
    POSTGRESQL_SEQUENCE_OWNER_TEMPLATE,
    POSTGRESQL_TABLE_CONSTRAINT_NAMES_SQL,
    POSTGRESQL_TABLE_SEQUENCE_NAMES_SQL,
)
from hare.fields.field import Field
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlTableRebuild(TableRebuild):
    """TableRebuild as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    async def replace_table(
        self, table_name: str, rebuilt_table_name: str, schema: str | None, fields: Iterable[Field[Any]]
    ) -> None:
        """Drops a table and renames its rebuilt copy into its place, then renames what PostgreSQL
        named after the copy's temporary name - its implicit constraints (``<table>_pkey``,
        ``<table>_<column>_key``, ``<table>_<column>_fkey``) and serial sequences - and writes the
        column comments the rebuilt table's definition collected.

        Args:
            table_name: The table being replaced.
            rebuilt_table_name: The already filled copy taking its place.
            schema: The tables' schema.
            fields: The fields of the rebuilt table's columns.
        """
        await super().replace_table(table_name, rebuilt_table_name, schema, fields)
        qualified_table = self.editor.qualify_table_name(table_name, schema)
        if not self.editor.collect_sql:
            temporary_prefix = f"{rebuilt_table_name}_"
            constraint_rows = await self.editor.client.execute_dicts(
                POSTGRESQL_TABLE_CONSTRAINT_NAMES_SQL, [table_name, schema]
            )
            for row in constraint_rows:
                if row["name"].startswith(temporary_prefix):
                    new_name = f"{table_name}_{row['name'].removeprefix(temporary_prefix)}"
                    await self.editor.run_sql(
                        f"ALTER TABLE {qualified_table} RENAME CONSTRAINT {self.editor.quote(row['name'])} "
                        f"TO {self.editor.quote(new_name)}"
                    )
            sequence_rows = await self.editor.client.execute_dicts(
                POSTGRESQL_TABLE_SEQUENCE_NAMES_SQL, [table_name, schema]
            )
            for row in sequence_rows:
                if row["name"].startswith(temporary_prefix):
                    new_name = f"{table_name}_{row['name'].removeprefix(temporary_prefix)}"
                    await self.editor.run_sql(
                        f"ALTER SEQUENCE {self.editor.qualify_table_name(row['name'], schema)} "
                        f"RENAME TO {self.editor.quote(new_name)}"
                    )
        if comments_sql := self.editor.table_creation.post_table_hook():
            await self.editor.run_sql(comments_sql.strip())

    async def remake_table(self, model: type[Model], *args: Any, **kwargs: Any) -> None:
        """Rebuilds a table as ``TableRebuild.remake_table()`` does, then sets its comment
        again - a PostgreSQL table comment belongs to the table, and went with the old one.

        Args:
            model: The model rendered from the state the table is rebuilt to.
            *args: The rebuild's other arguments.
            **kwargs: The rebuild's other keyword arguments.
        """
        meta = model._meta
        # The views read the old table and an owned sequence would be dropped with it; its row
        # level security, policies and grants go with it - all are put back on the new table.
        await self.editor.drop_views_of_table(model)
        for sequence in meta.sequences:
            if sequence.owned_by is not None:
                await self.editor.run_sql(
                    POSTGRESQL_SEQUENCE_OWNER_TEMPLATE.format(
                        sequence=self.editor.qualify_object_name(model, sequence.name),
                        owner=POSTGRESQL_SEQUENCE_NO_OWNER_SQL,
                    )
                )
        await super().remake_table(model, *args, **kwargs)
        if meta.table_description:
            await self.editor.table_comments.alter_table_comment(model)
        statements = [
            statement
            for sequence in meta.sequences
            for statement in self.editor.sequences.get_sequence_owner_sqls(model, sequence)
        ]
        statements += [
            statement for view in meta.views for statement in self.editor.views.get_view_create_sqls(model, view)
        ]
        statements += [
            statement
            for view in meta.materialized_views
            for statement in self.editor.materialized_views.get_materialized_view_create_sqls(model, view)
        ]
        if meta.row_level_security is not None:
            statements += self.editor.row_level_security_policies.get_row_level_security_sqls(
                model, None, meta.row_level_security
            )
        statements += [
            statement
            for policy in meta.policies
            for statement in self.editor.row_level_security_policies.get_policy_create_sqls(model, policy)
        ]
        statements += [
            statement
            for grant in meta.grants
            if grant.on != GrantTarget.FUNCTION
            for statement in self.editor.grants.get_grant_sqls(model, grant)
        ]
        await self.editor.run_sqls(statements)
