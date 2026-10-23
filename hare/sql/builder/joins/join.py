from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.constants import ARRAY_JOIN_TYPES
from hare.sql.enums import JoinType
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.selectable import Selectable
    from hare.sql.builder.tables.table import Table


class Join:
    """A JOIN of a query: the joined source and the type of the join."""

    def __init__(self, item: Selectable, how: JoinType) -> None:
        self.item = item
        self.how = how

    def get_sql(self, sql_context: SqlContext) -> str:
        """The JOIN clause.

        Args:
            sql_context: How the SQL is written - the dialect, quoting, aliases.

        Returns:
            The SQL.
        """
        join_context = sql_context.copy(subquery=True, with_alias=True)
        dialect = sql_context.dialect
        if sql_context.subquery and not dialect.features.table_alias_hides_name:
            # The joined table's own name is the enclosing query's - see get_joined_source_sql().
            item_sql = dialect.clauses.get_joined_source_sql(self.item, join_context)
        else:
            item_sql = self.item.get_sql(join_context)
        how = self.item.join_type or self.how
        # An ARRAY JOIN unfolds the row's own array - it reads no other table.
        prefix = "" if how in ARRAY_JOIN_TYPES else dialect.clauses.get_distributed_read_prefix_sql(sql_context)
        if how:
            return f"{prefix}{how} JOIN {item_sql}"
        return f"{prefix}JOIN {item_sql}"

    def validate(self, _from: Sequence[Table], _joins: Sequence[Table]) -> None:
        """Checks the join against the tables of the query - nothing for a join without a condition.

        Args:
            _from: The tables of the query's ``FROM``.
            _joins: The tables of its joins.
        """
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
