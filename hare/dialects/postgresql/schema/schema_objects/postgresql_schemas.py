from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.schema_objects.schemas import Schemas
from hare.dialects.postgresql.schema.constants import POSTGRESQL_MOVE_OBJECT_TO_CURRENT_SCHEMA_TEMPLATE

if TYPE_CHECKING:
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlSchemas(Schemas):
    """Schemas as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_table_schema_move_plan(
        self, table_name: str, old_schema: str | None, new_schema: str | None
    ) -> SchemaChangePlan:
        """Prepares moving a table with ``ALTER TABLE ... SET SCHEMA``, which keeps its
        rows, constraints and indexes.

        Args:
            table_name: The table.
            old_schema: The schema it is in; None for the connection's current schema.
            new_schema: The schema it moves to; None for the connection's current schema.
        """
        if (old_schema or None) == (new_schema or None):
            return SchemaChangePlan(())
        return self.get_object_schema_move_plan(
            "TABLE", self.editor.qualify_table_name(table_name, old_schema), new_schema
        )

    def get_object_schema_move_plan(
        self, object_type: str, qualified_name: str, new_schema: str | None
    ) -> SchemaChangePlan:
        """Prepares ``ALTER <type> <name> SET SCHEMA``, including a runtime current schema.

        Args:
            object_type: The object's type in SQL: ``TABLE``, ``VIEW``, ``MATERIALIZED VIEW``,
                ``SEQUENCE``, ``FUNCTION``.
            qualified_name: Its name in its schema - a function's with its argument types.
            new_schema: The schema it moves to; None for the connection's current schema.
        """
        if new_schema:
            return SchemaChangePlan(
                (SchemaStatement(f"ALTER {object_type} {qualified_name} SET SCHEMA {self.editor.quote(new_schema)};"),)
            )
        literals = self.editor.client.dialect.literals
        body = POSTGRESQL_MOVE_OBJECT_TO_CURRENT_SCHEMA_TEMPLATE.format(
            object_type=object_type, name_literal=literals.get_string_literal_sql(qualified_name)
        )
        return SchemaChangePlan((SchemaStatement(f"{self.editor.get_anonymous_block_sql(body)};"),))
