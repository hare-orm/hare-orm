from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table
from hare.sql.terms.criteria.range_criterion import RangeCriterion


class BetweenCriterion(RangeCriterion):
    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
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
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        term_sql = self.term.get_sql(sql_context)
        sql = f"{term_sql} BETWEEN {self.start.get_sql(sql_context)} AND {self.end.get_sql(sql_context)}"
        return sql_context.format_alias_sql(sql, self.alias)
