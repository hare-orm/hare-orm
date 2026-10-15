from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import TNode

if TYPE_CHECKING:
    from hare.sql.builder.tables.selectable import Selectable
from hare.sql.terms.field import Field


class Star(Field):
    def __init__(self, table: str | Selectable | None = None) -> None:
        super().__init__("*", table=table)

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        if self.table is not None:
            yield from self.table.nodes_()

    def get_sql(self, sql_context: SqlContext) -> str:
        if self.table and (sql_context.with_namespace or self.table.alias):
            namespace = self.table.alias or getattr(self.table, "_table_name")
            return f"{sql_context.quote(namespace)}.*"

        return "*"
