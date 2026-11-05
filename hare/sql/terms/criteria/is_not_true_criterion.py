from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import Node


class IsNotTrueCriterion(Criterion):
    """``(<term>) IS NOT TRUE`` - FALSE only when ``term`` is TRUE, TRUE when it is FALSE or NULL. The
    term is always parenthesized; a dialect without ``IS NOT TRUE`` writes its own form.
    """

    structure_attributes = ("term",)

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__(alias=alias)
        self.term = term

    def get_children(self) -> Iterator[Node]:
        yield self.term

    def get_sql(self, sql_context: SqlContext) -> str:
        renderer = sql_context.dialect.renderers.get(type(self))
        sql = (
            renderer(self, sql_context) if renderer is not None else f"({self.term.get_sql(sql_context)}) IS NOT TRUE"
        )
        return sql_context.format_alias_sql(sql, self.alias)
