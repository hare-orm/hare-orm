from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function


class SqliteCheckedInteger(Function):
    """An integer result of arithmetic refused past its range on hare's own connections
    (``SQLITE_INTEGER_IN_RANGE_FUNCTION_NAME``) - SQL kept in DDL can't call that function, the result is
    then the database's own."""

    def get_function_sql(self, sql_context: SqlContext) -> str:
        if sql_context.native_functions_only:
            return self.get_arg_sql(self.args[0], sql_context)
        return super().get_function_sql(sql_context)
