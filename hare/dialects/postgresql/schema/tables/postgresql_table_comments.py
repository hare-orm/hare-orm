from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.fields.field import Field
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlTableComments(TableComments):
    """TableComments as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        sql = self.editor.TABLE_COMMENT_TEMPLATE.format(
            table=table, comment=self.editor.client.dialect.literals.get_string_literal_sql(comment)
        )
        if sql not in self.editor.comments_array:
            self.editor.comments_array.append(sql)
        return ""

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        sql = self.editor.COLUMN_COMMENT_TEMPLATE.format(
            table=table,
            column=self.editor.quote(column),
            comment=self.editor.client.dialect.literals.get_string_literal_sql(comment),
        )
        if sql not in self.editor.comments_array:
            self.editor.comments_array.append(sql)
        return ""

    async def alter_column_comment(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> None:
        """Emit COMMENT ON COLUMN for PostgreSQL."""
        db_field = new_field.source_field or new_field.model_field_name
        qualified_table = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        if new_field.description:
            comment = self.editor.client.dialect.literals.get_string_literal_sql(new_field.description)
            await self.editor.run_sql(
                self.editor.COLUMN_COMMENT_TEMPLATE.format(
                    table=qualified_table, column=self.editor.quote(db_field), comment=comment
                )
            )
        else:
            # Remove comment: SET NULL
            await self.editor.run_sql(f"COMMENT ON COLUMN {qualified_table}.{self.editor.quote(db_field)} IS NULL;")

    async def alter_table_comment(self, model: type[Model]) -> None:
        """Sets the table comment to the model's ``table_description`` (removes it when empty).

        Args:
            model: The model, rendered with its new description.
        """
        qualified_table = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        if model._meta.table_description:
            await self.editor.run_sql(
                self.editor.TABLE_COMMENT_TEMPLATE.format(
                    table=qualified_table,
                    comment=self.editor.client.dialect.literals.get_string_literal_sql(model._meta.table_description),
                )
            )
        else:
            await self.editor.run_sql(f"COMMENT ON TABLE {qualified_table} IS NULL;")
