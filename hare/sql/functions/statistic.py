from __future__ import annotations

from hare.sql.functions.distinct_option_function import DistinctOptionFunction
from hare.sql.sql_context import SqlContext


class Statistic(DistinctOptionFunction):
    """A Postgres statistic aggregate (``STDDEV_POP``, ``VAR_SAMP``, ...) - a hare UDF of the same
    semantics on SQLite, which has none."""

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return self.get_statistic_sql(self.name, sql_context)

    def get_statistic_sql(self, name: str, sql_context: SqlContext) -> str:
        """The aggregate called by ``name``."""
        args_sql = ",".join(self.get_arg_sql(arg, sql_context) for arg in self.args)
        sql = f"{name}({'DISTINCT ' if self._distinct else ''}{args_sql})"
        if self._include_filter:
            sql += f" FILTER({self.get_filter_sql(sql_context)})"
        return sql
