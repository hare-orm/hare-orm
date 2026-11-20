from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.values.value_wrapper import ValueWrapper


class PaginationSql:
    """The clauses bounding the rows of a SELECT and of a set operation - written by the dialect
    (``QueryClauses.get_limit_offset_sql()``)."""

    @staticmethod
    def get_sql(limit: ValueWrapper | None, offset: ValueWrapper | None, sql_context: SqlContext) -> str:
        """Render the supplied bounds through the selected dialect's clause builder."""
        limit_sql = limit.get_sql(sql_context) if limit is not None else None
        offset_sql = offset.get_sql(sql_context) if offset is not None else None
        if limit_sql is None and offset_sql is None:
            return ""
        return sql_context.dialect.clauses.get_limit_offset_sql(limit_sql, offset_sql)
