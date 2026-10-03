from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.table import Table
from hare.sql.terms.criteria.range_criterion import RangeCriterion


class BetweenCriterion(RangeCriterion):
    @builder
    def replace_table(  # type:ignore[return]
        self, current_table: Table | None, new_table: Table | None
    ) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the criterion with the tables replaced.
        """
        self.term = self.term.replace_table(current_table, new_table)
        self.start = self.start.replace_table(current_table, new_table)
        self.end = self.end.replace_table(current_table, new_table)

    def get_sql(self, ctx: SqlContext) -> str:
        # FIXME escape
        sql = f"{self.term.get_sql(ctx)} BETWEEN {self.start.get_sql(ctx)} AND {self.end.get_sql(ctx)}"
        return ctx.format_alias_sql(sql, self.alias)
