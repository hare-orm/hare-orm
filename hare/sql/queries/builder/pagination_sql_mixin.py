from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    pass


class PaginationSqlMixin:
    """The LIMIT and OFFSET clauses of a SELECT and of a set operation. An OFFSET without a LIMIT
    gets the dialect's unbounded LIMIT when its SQL needs one (SQLite's ``LIMIT -1``)."""

    _limit: ValueWrapper | None
    _offset: ValueWrapper | None

    def _offset_sql(self, ctx: SqlContext) -> str:
        if self._offset is None:
            return ""
        return f" OFFSET {self._offset.get_sql(ctx)}"

    def _limit_sql(self, ctx: SqlContext) -> str:
        if self._limit is None:
            unbounded_limit_sql = ctx.dialect.get_unbounded_limit_sql()
            if self._offset is not None and unbounded_limit_sql is not None:
                return f" LIMIT {unbounded_limit_sql}"
            return ""
        return f" LIMIT {self._limit.get_sql(ctx)}"
