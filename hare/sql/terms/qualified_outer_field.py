from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Iterator
    from typing import Self

    from hare.sql.builder.tables.table import Table
    from hare.sql.terms.node import TNode


class QualifiedOuterField(Term):
    """ "table"."column" - always table-qualified, unlike a plain Field/table[column], which the
    child QueryBuilder could silently re-resolve against its own table."""

    def __init__(self, table: Table, column: str) -> None:
        super().__init__()
        self.table = table
        self.column = column

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.table.nodes_()

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces every occurrence of a table with another one.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the term with the tables replaced.
        """
        if self.table == current_table:
            self.table = new_table  # type:ignore[assignment]
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        table_name = self.table.get_table_name()
        sql = f"{sql_context.quote(table_name)}.{sql_context.quote(self.column)}"
        if sql_context.with_alias and self.alias:
            return sql_context.format_alias_sql(sql, self.alias)
        return sql
