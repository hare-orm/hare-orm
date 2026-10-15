from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, ClassVar

from hare.sql.builder_methods import BuilderMethods
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Self

    from hare.sql.builder.tables.table import Table
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.node import TNode


class GroupingElement(Term):
    """An element of ``GROUP BY`` standing for several grouping sets of its terms (SQL:1999) - the
    ``ROLLUP``/``CUBE`` of a subclass.

    Args:
        terms: The grouped terms.
    """

    keyword: ClassVar[str]

    def __init__(self, *terms: Term) -> None:
        super().__init__()
        self.terms = list(terms)

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        for term in self.terms:
            yield from term.nodes_()

    def get_sql(self, sql_context: SqlContext) -> str:
        return f"{self.keyword}({','.join(term.get_sql(sql_context) for term in self.terms)})"

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        self.terms = [term.replace_table(current_table, new_table) for term in self.terms]
        return self
