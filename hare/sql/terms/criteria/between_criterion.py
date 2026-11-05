from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.range_criterion import RangeCriterion


class BetweenCriterion(RangeCriterion):
    """``term BETWEEN start AND end``."""

    structure_attributes = ("end", "start", "term")

    def get_sql(self, sql_context: SqlContext) -> str:
        term_sql = self.term.get_sql(sql_context)
        sql = f"{term_sql} BETWEEN {self.start.get_sql(sql_context)} AND {self.end.get_sql(sql_context)}"
        return sql_context.format_alias_sql(sql, self.alias)
