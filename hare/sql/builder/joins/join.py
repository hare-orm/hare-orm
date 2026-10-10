from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.enums import JoinType
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.selectable import Selectable
    from hare.sql.builder.tables.table import Table


class Join:
    def __init__(self, item: Selectable, how: JoinType) -> None:
        self.item = item
        self.how = how

    def get_sql(self, sql_context: SqlContext) -> str:
        join_context = sql_context.copy(subquery=True, with_alias=True)
        sql = f"JOIN {self.item.get_sql(join_context)}"
        how = self.item.join_type or self.how
        if how:
            return f"{how} {sql}"
        return sql

    def validate(self, _from: Sequence[Table], _joins: Sequence[Table]) -> None:
        pass

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the join with the tables replaced.
        """
        self.item = self.item.replace_table(current_table, new_table)
        return self
