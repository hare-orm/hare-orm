from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.fields.field import Field

if TYPE_CHECKING:
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
    from hare.models.model_binding import ModelBinding


class PostgresqlTableComments(TableComments):
    """TableComments as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        # A statement of its own - get_table_comment_statements().
        return ""

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        # A statement of its own - get_column_comment_statements().
        return ""

    def get_table_comment_statements(self, table: str, comment: str) -> list[str]:
        return [
            self.editor.TABLE_COMMENT_TEMPLATE.format(
                table=table, comment=self.editor.client.dialect.literals.get_string_literal_sql(comment)
            )
        ]

    def get_column_comment_statements(self, table: str, column: str, comment: str) -> list[str]:
        return [
            self.editor.COLUMN_COMMENT_TEMPLATE.format(
                table=table,
                column=self.editor.quote(column),
                comment=self.editor.client.dialect.literals.get_string_literal_sql(comment),
            )
        ]

    def get_column_comment_change_sqls(self, binding: ModelBinding, new_field: Field[Any]) -> list[str]:
        """Set a column comment, or remove it with SQL NULL when empty."""
        column = self.editor.quote(new_field.source_field or new_field.model_field_name)
        table = self.editor.qualify_table_name(binding.db_table, binding.schema)
        comment = (
            self.editor.client.dialect.literals.get_string_literal_sql(new_field.description)
            if new_field.description
            else "NULL"
        )
        return [self.editor.COLUMN_COMMENT_TEMPLATE.format(table=table, column=column, comment=comment)]

    def get_table_comment_plan(self, old_binding: ModelBinding, new_binding: ModelBinding) -> SchemaChangePlan:
        """Compile setting a table comment, or removing it when the description is empty."""
        qualified_table = self.editor.qualify_table_name(new_binding.db_table, new_binding.schema)
        if new_binding.table_description:
            sql = self.editor.TABLE_COMMENT_TEMPLATE.format(
                table=qualified_table,
                comment=self.editor.client.dialect.literals.get_string_literal_sql(new_binding.table_description),
            )
        else:
            sql = f"COMMENT ON TABLE {qualified_table} IS NULL;"
        return SchemaChangePlan((SchemaStatement(sql),))
