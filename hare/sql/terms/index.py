from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:
    pass


class Index(Term):
    def __init__(self, name: str, alias: str | None = None) -> None:
        super().__init__(alias)
        self.name = name

    def get_sql(self, ctx: SqlContext) -> str:
        return ctx.quote(self.name)
