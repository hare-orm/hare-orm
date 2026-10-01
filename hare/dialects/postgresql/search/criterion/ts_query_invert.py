from collections.abc import Iterator

from hare.sql.terms.base.node import TNode
from hare.sql.terms.base.term import Term


class TsQueryInvert(Term):
    def __init__(self, term: Term) -> None:
        super().__init__()
        self.term = term

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.term.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return self.term.is_aggregate

    def get_sql(self, ctx) -> str:
        sql = f"!!({self.term.get_sql(ctx)})"
        if ctx.with_alias and self.alias:  # pragma: nocoverage
            return f'{sql} "{self.alias}"'
        return sql
