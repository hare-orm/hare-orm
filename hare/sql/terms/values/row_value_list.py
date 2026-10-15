from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.tuple import Tuple


class RowValueList(Tuple):
    """The rows a row value is compared with by ``IN`` - ``((1, 2), (3, 4))``; a dialect that takes no
    list of row values there writes its own form."""

    def get_sql(self, sql_context: SqlContext) -> str:
        renderer = sql_context.dialect.renderers.get(type(self))
        if renderer is not None:
            return renderer(self, sql_context)
        return super().get_sql(sql_context)
