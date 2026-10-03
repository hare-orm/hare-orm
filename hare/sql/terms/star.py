from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.terms.base.node import TNode

if TYPE_CHECKING:
    from hare.sql.queries.tables.selectable import Selectable
from hare.sql.terms.field import Field


class Star(Field):
    def __init__(self, table: str | Selectable | None = None) -> None:
        super().__init__("*", table=table)

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        if self.table is not None:
            yield from self.table.nodes_()

    def get_sql(self, ctx: SqlContext) -> str:
        if self.table and (ctx.with_namespace or self.table.alias):
            namespace = self.table.alias or getattr(self.table, "_table_name")
            return f"{ctx.quote(namespace)}.*"

        return "*"
