from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Self

    from hare.sql.builder.tables.table import Table


class FullTextMatch(Criterion):
    """A row the full-text index matches the query for - ``key IN (SELECT rowid FROM index WHERE
    index MATCH query)``.

    Args:
        row_key: The column of the row's integer primary key, which keys the index's rows.
        index_table_name: The FTS5 table.
        query: The search query, restricted to the searched columns.
    """

    def __init__(self, row_key: Term, index_table_name: str, query: Term, alias: str | None = None) -> None:
        super().__init__(alias)
        self.row_key = row_key
        self.index_table_name = index_table_name
        self.query = query

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.row_key.nodes_()
        yield from self.query.nodes_()

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces the table of the row's key column.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy with the table replaced.
        """
        self.row_key = self.row_key.replace_table(current_table, new_table)
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        index_table_sql = sql_context.quote(self.index_table_name)
        row_key_sql = self.row_key.get_sql(sql_context.copy(with_alias=False))
        query_sql = self.query.get_sql(sql_context.copy(with_alias=False))
        sql = f"{row_key_sql} IN (SELECT rowid FROM {index_table_sql} WHERE {index_table_sql} MATCH {query_sql})"  # nosec B608
        return sql_context.format_alias_sql(sql, self.alias)
