from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.context import SqlContext

if TYPE_CHECKING:
    pass
from hare.sql.functions.distinct_option_function import DistinctOptionFunction


class Statistic(DistinctOptionFunction):
    """A Postgres statistic aggregate (``STDDEV_POP``, ``VAR_SAMP``, ...) - a hare UDF of the same
    semantics on SQLite, which has none."""

    def get_function_sql(self, ctx: SqlContext) -> str:
        return self.get_statistic_sql(self.name, ctx)

    def get_statistic_sql(self, name: str, ctx: SqlContext) -> str:
        """The aggregate called by ``name``."""
        args_sql = ",".join(self.get_arg_sql(arg, ctx) for arg in self.args)
        sql = f"{name}({'DISTINCT ' if self._distinct else ''}{args_sql})"
        if self._include_filter:
            sql += f" FILTER({self.get_filter_sql(ctx)})"
        return sql
