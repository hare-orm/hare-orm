from __future__ import annotations

from typing import Any

from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_COLUMN_COMMENT_CHANGE_TEMPLATE,
    CLICKHOUSE_TABLE_COMMENT_CHANGE_TEMPLATE,
)
from hare.fields.field import Field
from hare.models import Model


class ClickhouseTableComments(TableComments):
    """The comments of a table and its columns as ClickHouse writes them - ``COMMENT '...'`` after
    the column's type, and after the table's engine; changed by ``COMMENT COLUMN`` and
    ``MODIFY COMMENT``, an empty comment for none."""

    __slots__ = ()

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return f" COMMENT {self.editor.client.dialect.literals.get_string_literal_sql(comment)}"

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return f" COMMENT {self.editor.client.dialect.literals.get_string_literal_sql(comment)}"

    def get_column_comment_change_sqls(self, model: type[Model], new_field: Field[Any]) -> list[str]:
        """Prepare the comment change, using an empty string to remove the comment."""
        return [
            self.editor.get_column_change_sql(
                model,
                CLICKHOUSE_COLUMN_COMMENT_CHANGE_TEMPLATE,
                column=self.editor.quote(new_field.source_field or new_field.model_field_name),
                comment=self.editor.client.dialect.literals.get_string_literal_sql(new_field.description or ""),
            )
        ]

    def get_table_comment_plan(self, old_model: type[Model], new_model: type[Model]) -> SchemaChangePlan:
        """Compile the comment change for the declared storage and companion tables."""
        literals = self.editor.client.dialect.literals
        sql = self.editor.get_column_change_sql(
            new_model,
            CLICKHOUSE_TABLE_COMMENT_CHANGE_TEMPLATE,
            comment=literals.get_string_literal_sql(new_model._meta.table_description or ""),
        )
        return SchemaChangePlan((SchemaStatement(sql, recovery=self.editor.get_schema_recovery(new_model)),))
