from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.json import JSON
from hare.sql.terms.node import TNode

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.selectable import Selectable
    from hare.sql.builder.tables.table import Table


class Field(Criterion, JSON):
    def __init__(
        self,
        name: str,
        alias: str | None = None,
        table: str | Selectable | None = None,
    ) -> None:
        super().__init__(alias=alias)
        self.name = name
        self.table = table  # type:ignore[assignment]

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        if self.table is not None:
            yield from self.table.nodes_()

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the field with the tables replaced.
        """
        if self.table == current_table:
            self.table = new_table
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        field_sql = sql_context.quote(self.name)

        # Need to add namespace if the table has an alias
        if self.table and (sql_context.with_namespace or self.table.alias):
            table_name = self.table.get_table_name()
            field_sql = f"{sql_context.quote(table_name)}.{field_sql}"

        field_alias = getattr(self, "alias", None)
        if sql_context.with_alias:
            return sql_context.format_alias_sql(field_sql, field_alias)
        return field_sql
