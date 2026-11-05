from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import Node

if TYPE_CHECKING:
    from hare.sql.builder.tables.selectable import Selectable
from hare.sql.terms.field import Field


class Star(Field):
    """``*`` - every column, of one table when it is given."""

    structure_attributes = ()

    def __init__(self, table: str | Selectable | None = None) -> None:
        super().__init__("*", table=table)

    def get_children(self) -> Iterator[Node]:
        if self.table is not None:
            yield self.table

    def get_sql(self, sql_context: SqlContext) -> str:
        if self.table is not None:
            namespace = sql_context.source_aliases.get(self.table.source_binding, self.table.get_table_name())
            if (
                sql_context.with_namespace
                or self.table.alias
                or self.table.source_binding in sql_context.source_aliases
            ):
                return f"{sql_context.quote(namespace)}.*"

        return "*"
