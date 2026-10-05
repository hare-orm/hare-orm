from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.values.value_wrapper import ValueWrapper


class PaginationSql:
    """The clauses bounding the rows of a SELECT and of a set operation - written by the dialect
    (``QueryClauses.get_limit_offset_sql()``)."""

    _limit: ValueWrapper | None
    _offset: ValueWrapper | None

    def _limit_offset_sql(self, sql_context: SqlContext) -> str:
        limit_sql = self._limit.get_sql(sql_context) if self._limit is not None else None
        offset_sql = self._offset.get_sql(sql_context) if self._offset is not None else None
        if limit_sql is None and offset_sql is None:
            return ""
        return sql_context.dialect.clauses.get_limit_offset_sql(limit_sql, offset_sql)
