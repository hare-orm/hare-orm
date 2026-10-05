from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.terms.criteria.basic_criterion import BasicCriterion

if TYPE_CHECKING:
    from hare.sql.sql_context import SqlContext


class SqliteRegexCriterion(BasicCriterion):
    """``text REGEXP pattern`` or ``text MATCH pattern`` - SQLite runs the operators through the
    ``regexp()``/``match()`` functions hare registers on its own connections, noted in the context the
    criterion is written in (``SqlContext.note_connection_function()``)."""

    def get_sql(self, sql_context: SqlContext) -> str:
        sql_context.note_connection_function(str(self.comparator).strip().lower())
        return super().get_sql(sql_context)
