from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.context import SqlContext

if TYPE_CHECKING:
    pass
from hare.sql.functions.collate import Collate


class DecimalTextCollate(Collate):
    """A DecimalField column's text compared by an exact-decimal collation, on a dialect storing a
    Decimal as text. SQL kept in DDL or a migration can't name the collation - a Decimal compares as
    a number there.
    """

    def get_function_sql(self, ctx: SqlContext) -> str:
        if ctx.native_functions_only:
            return f"CAST({self.get_arg_sql(self.args[0], ctx)} AS NUMERIC)"
        return super().get_function_sql(ctx)
