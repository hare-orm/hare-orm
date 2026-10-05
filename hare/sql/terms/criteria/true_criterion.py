from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion


class TrueCriterion(Criterion):
    """``TRUE`` - the condition of a JOIN keeping every pair of rows, such as a ``LATERAL`` subquery's."""

    is_aggregate = None

    def get_sql(self, sql_context: SqlContext) -> str:
        return sql_context.format_alias_sql("TRUE", self.alias)
