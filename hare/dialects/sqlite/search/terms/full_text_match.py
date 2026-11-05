from __future__ import annotations

from collections.abc import Iterator

from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term


class FullTextMatch(Criterion):
    """A row the full-text index matches the query for - ``key IN (SELECT rowid FROM index WHERE
    index MATCH query)``.

    Args:
        row_key: The column of the row's integer primary key, which keys the index's rows.
        index_table_name: The FTS5 table.
        index_schema: The FTS5 table's attached database; None for the main one.
        query: The search query, restricted to the searched columns.
    """

    structure_attributes = ("row_key", "query", "index_table_name", "index_schema")

    def __init__(
        self,
        row_key: Term,
        index_table_name: str,
        query: Term,
        index_schema: str | None = None,
        alias: str | None = None,
    ) -> None:
        super().__init__(alias)
        self.row_key = row_key
        self.index_table_name = index_table_name
        self.index_schema = index_schema
        self.query = query

    def get_children(self) -> Iterator[Node]:
        yield self.row_key
        yield self.query

    def get_sql(self, sql_context: SqlContext) -> str:
        index_table_sql = sql_context.quote(self.index_table_name)
        qualified_index_table_sql = (
            f"{sql_context.quote(self.index_schema)}.{index_table_sql}" if self.index_schema else index_table_sql
        )
        row_key_sql = self.row_key.get_sql(sql_context.copy(with_alias=False))
        query_sql = self.query.get_sql(sql_context.copy(with_alias=False))
        sql = (
            f"{row_key_sql} IN (SELECT rowid FROM {qualified_index_table_sql} "  # nosec B608
            f"WHERE {index_table_sql} MATCH {query_sql})"
        )
        return sql_context.format_alias_sql(sql, self.alias) if sql_context.with_alias else sql
