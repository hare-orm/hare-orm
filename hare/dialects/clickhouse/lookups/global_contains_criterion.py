from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.contains_criterion import ContainsCriterion


class GlobalContainsCriterion(ContainsCriterion):
    """``term GLOBAL [NOT] IN container`` - ClickHouse reads the list or subquery once, on the server
    the query was sent to, and sends it to every shard of a ``Distributed`` table."""

    def get_sql(self, sql_context: SqlContext) -> str:
        container_context = sql_context.copy(subquery=True)
        not_sql = "NOT " if self._is_negated else ""
        sql = f"{self.term.get_sql(sql_context)} GLOBAL {not_sql}IN {self.container.get_sql(container_context)}"
        return sql_context.format_alias_sql(sql, self.alias)
