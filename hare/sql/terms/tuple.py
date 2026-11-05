from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term


class Tuple(Criterion):
    """A row of values - ``(a, b, ...)``."""

    structure_attributes: ClassVar[tuple[str, ...]] = ("values",)

    def __init__(self, *values: Any) -> None:
        super().__init__()
        self.values = [self.wrap_constant(value) for value in values]

    def get_children(self) -> Iterator[Node]:
        yield from self.values

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = "({})".format(",".join(term.get_sql(sql_context) for term in self.values))
        return sql_context.format_alias_sql(sql, self.alias)

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return Term.get_combined_is_aggregate([value.is_aggregate for value in self.values])
