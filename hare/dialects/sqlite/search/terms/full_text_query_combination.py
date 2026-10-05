from __future__ import annotations

from collections.abc import Iterator
from copy import copy
from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Self

    from hare.sql.builder.tables.table import Table


class FullTextQueryCombination(Term):
    """Two FTS5 queries joined - ``(left) AND (right)``, ``OR`` or ``NOT`` - by concatenating their
    texts in SQL.

    Args:
        left: The left query.
        operator: ``AND``, ``OR`` or ``NOT``.
        right: The right query.
        column_filter: The FTS5 column filter the combination is restricted by, empty for every
            column of the index.
    """

    def __init__(
        self, left: Term, operator: str, right: Term, column_filter: str = "", alias: str | None = None
    ) -> None:
        super().__init__(alias)
        self.left = left
        self.operator = operator
        self.right = right
        self.column_filter = column_filter

    def with_column_filter(self, column_filter: str) -> FullTextQueryCombination:
        """A copy restricted to the columns of an FTS5 column filter.

        Args:
            column_filter: The filter, empty for every column of the index.

        Returns:
            The copy.
        """
        restricted_query = copy(self)
        restricted_query.column_filter = column_filter
        return restricted_query

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.left.nodes_()
        yield from self.right.nodes_()

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces the table of both queries' terms.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy with the table replaced.
        """
        self.left = self.left.replace_table(current_table, new_table)
        self.right = self.right.replace_table(current_table, new_table)
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        inner_context = sql_context.copy(with_alias=False)
        left_sql = self.left.get_sql(inner_context)
        right_sql = self.right.get_sql(inner_context)
        operator_sql = SqlContext.quote_text(f") {self.operator} (", "'")
        sql = f"'(' || {left_sql} || {operator_sql} || {right_sql} || ')'"
        if self.column_filter:
            column_filter_sql = SqlContext.quote_text(f"{self.column_filter} : (", "'")
            sql = f"{column_filter_sql} || {sql} || ')'"
        sql = f"({sql})"
        return sql_context.format_alias_sql(sql, self.alias) if sql_context.with_alias else sql
