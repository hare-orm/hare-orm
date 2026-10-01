from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.enums import JoinType
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.selectable import Selectable
    from hare.sql.queries.tables.table import Table


class Join:
    def __init__(self, item: Selectable, how: JoinType) -> None:
        self.item = item
        self.how = how

    def get_sql(self, ctx: SqlContext) -> str:
        join_ctx = ctx.copy(subquery=True, with_alias=True)
        sql = f"JOIN {self.item.get_sql(join_ctx)}"

        if self.how:
            return f"{self.how} {sql}"
        return sql

    def validate(self, _from: Sequence[Table], _joins: Sequence[Table]) -> None:
        pass

    @builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:  # type:ignore[return]
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the join with the tables replaced.
        """
        self.item = self.item.replace_table(current_table, new_table)
