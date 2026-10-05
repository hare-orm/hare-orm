from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Self

    from hare.sql.builder.tables.table import Table


class FullTextRowValue(Term):
    """What an FTS5 auxiliary function (``bm25()``, ``highlight()``, ``snippet()``) gives for the
    row the query reads - a subquery of the full-text index matching the query for that row. A
    subclass writes the function.

    Args:
        row_key: The column of the row's integer primary key, which keys the index's rows.
        index_table_name: The FTS5 table.
        query: The search query.
    """

    is_subquery = True

    def __init__(self, row_key: Term, index_table_name: str, query: Term, alias: str | None = None) -> None:
        super().__init__(alias)
        self.row_key = row_key
        self.index_table_name = index_table_name
        self.query = query

    @property
    def outer_reference_terms(self) -> tuple[Term, ...]:  # type: ignore[override]
        return (self.row_key,)

    def get_value_terms(self) -> tuple[Term, ...]:
        """The terms the function and the value for a row the index doesn't match read.

        Returns:
            The terms - none by default.
        """
        return ()

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.row_key.nodes_()
        yield from self.query.nodes_()
        for value_term in self.get_value_terms():
            yield from value_term.nodes_()

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

    def get_function_sql(self, sql_context: SqlContext, index_table_sql: str) -> str:
        """Returns the auxiliary function's call.

        Args:
            sql_context: The context.
            index_table_sql: The quoted FTS5 table.

        Returns:
            The SQL.
        """
        raise NotImplementedError

    def get_unmatched_value_sql(self, sql_context: SqlContext) -> str | None:
        """Returns the value of a row the index doesn't match.

        Args:
            sql_context: The context.

        Returns:
            The SQL, None for NULL.
        """
        return None

    def get_sql(self, sql_context: SqlContext) -> str:
        inner_context = sql_context.copy(with_alias=False)
        index_table_sql = sql_context.quote(self.index_table_name)
        function_sql = self.get_function_sql(inner_context, index_table_sql)
        query_sql = self.query.get_sql(inner_context)
        row_key_sql = self.row_key.get_sql(inner_context.copy(with_namespace=True))
        sql = (
            f"(SELECT {function_sql} FROM {index_table_sql} WHERE {index_table_sql} MATCH {query_sql} "  # nosec B608
            f"AND {index_table_sql}.rowid = {row_key_sql})"
        )
        unmatched_value_sql = self.get_unmatched_value_sql(inner_context)
        if unmatched_value_sql is not None:
            sql = f"COALESCE({sql},{unmatched_value_sql})"
        return sql_context.format_alias_sql(sql, self.alias) if sql_context.with_alias else sql
