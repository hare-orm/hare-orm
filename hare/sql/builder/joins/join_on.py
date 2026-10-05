from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.enums import JoinType
from hare.sql.exceptions import JoinException
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.builder.tables.selectable import Selectable
    from hare.sql.builder.tables.table import Table
from hare.sql.builder.joins.join import Join


class JoinOn(Join):
    def __init__(
        self,
        item: Selectable,
        how: JoinType,
        criteria: QueryBuilder,
        collate: str | None = None,
    ) -> None:
        super().__init__(item, how)
        self.criterion = criteria
        self.collate = collate

    def get_sql(self, sql_context: SqlContext) -> str:
        join_sql = super().get_sql(sql_context)
        if self.item.joined_without_condition:
            return join_sql
        criterion_context = sql_context.copy(subquery=True)
        return "{join} ON {criterion}{collate}".format(
            join=join_sql,
            criterion=self.criterion.get_sql(criterion_context),
            collate=f" COLLATE {self.collate}" if self.collate else "",
        )

    def validate(self, _from: Sequence[Table], _joins: Sequence[Table]) -> None:
        criterion_tables = set([field.table for field in self.criterion.fields_()])
        available_tables = set(_from) | {join.item for join in _joins} | {self.item}
        missing_tables = criterion_tables - available_tables  # type:ignore[operator]
        if missing_tables:
            raise JoinException(
                "Invalid join criterion. One field is required from the joined item and "
                "another from the selected table or an existing join.  Found [{tables}]".format(
                    tables=", ".join(map(str, missing_tables))
                )
            )

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
        if self.item == current_table:
            self.item = new_table  # type:ignore[assignment]
        self.criterion = self.criterion.replace_table(current_table, new_table)
        return self
