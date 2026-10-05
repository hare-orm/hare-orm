from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.sql_context import SqlContext


class OldRowValue(Term):
    """A column of a written row as it was before the write, in its ``RETURNING`` - each dialect
    writes its own SQL.

    Args:
        column_name: The column.
        alias: The name the value is returned under.
    """

    def __init__(self, column_name: str, alias: str | None = None) -> None:
        super().__init__(alias)
        self.column_name = column_name

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = sql_context.dialect.clauses.get_old_row_value_sql(sql_context.quote(self.column_name))
        if sql_context.with_alias:
            return sql_context.format_alias_sql(sql, self.alias)
        return sql
