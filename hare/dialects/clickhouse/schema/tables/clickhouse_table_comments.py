from __future__ import annotations

from hare.dialects.base.schema.tables.table_comments import TableComments


class ClickhouseTableComments(TableComments):
    """The comments of a table and its columns as ClickHouse writes them - ``COMMENT '...'`` after
    the column's type, and after the table's engine."""

    __slots__ = ()

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return f" COMMENT {self.editor.client.dialect.literals.get_string_literal_sql(comment)}"

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return f" COMMENT {self.editor.client.dialect.literals.get_string_literal_sql(comment)}"
