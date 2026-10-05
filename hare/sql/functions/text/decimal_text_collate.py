from __future__ import annotations

from hare.sql.functions.text.collate import Collate
from hare.sql.sql_context import SqlContext


class DecimalTextCollate(Collate):
    """A DecimalField column's text compared by an exact-decimal collation, on a dialect storing a
    Decimal as text. SQL kept in DDL or a migration can't name the collation - a Decimal compares as
    a number there.
    """

    def get_function_sql(self, sql_context: SqlContext) -> str:
        if sql_context.native_functions_only:
            return f"CAST({self.get_arg_sql(self.args[0], sql_context)} AS NUMERIC)"
        return super().get_function_sql(sql_context)
