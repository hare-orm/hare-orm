from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.terms.base.json import JSON
from hare.sql.terms.base.node import TNode
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.selectable import Selectable
    from hare.sql.queries.tables.table import Table


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
            A copy of the field with the tables replaced.
        """
        if self.table == current_table:
            self.table = new_table

    def get_sql(self, ctx: SqlContext) -> str:
        field_sql = ctx.quote(self.name)

        # Need to add namespace if the table has an alias
        if self.table and (ctx.with_namespace or self.table.alias):
            table_name = self.table.get_table_name()
            field_sql = f"{ctx.quote(table_name)}.{field_sql}"

        field_alias = getattr(self, "alias", None)
        if ctx.with_alias:
            return ctx.format_alias_sql(field_sql, field_alias)
        return field_sql
