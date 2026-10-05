from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    from hare.sql.sql_context import SqlContext


class SqliteConnectionFunction(Function):
    """A call to a function hare registers on its own SQLite connections - noted in the context it is
    written in (``SqlContext.note_connection_function()``): SQL a database keeps can't call it."""

    def get_function_sql(self, sql_context: SqlContext) -> str:
        sql_context.note_connection_function(str(self.name))
        return super().get_function_sql(sql_context)
