from __future__ import annotations

from typing import Any

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

    async def alter_column_comment(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> None:
        await self.editor.run_sql(
            CLICKHOUSE_COLUMN_COMMENT_CHANGE_TEMPLATE.format(
                table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
                column=self.editor.quote(new_field.source_field or new_field.model_field_name),
                comment=self.editor.client.dialect.literals.get_string_literal_sql(new_field.description or ""),
            )
        )

    async def alter_table_comment(self, model: type[Model]) -> None:
        literals = self.editor.client.dialect.literals
        await self.editor.run_sql(
            CLICKHOUSE_TABLE_COMMENT_CHANGE_TEMPLATE.format(
                table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
                comment=literals.get_string_literal_sql(model._meta.table_description or ""),
            )
        )
