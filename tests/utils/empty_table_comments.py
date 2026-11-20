from hare.dialects.base.schema.tables.table_comments import TableComments


class EmptyTableComments(TableComments):
    __test__ = False

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return ""

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return ""
