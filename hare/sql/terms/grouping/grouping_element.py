from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, ClassVar

from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.node import Node


class GroupingElement(Term):
    """An element of ``GROUP BY`` standing for several grouping sets of its terms (SQL:1999) - the
    ``ROLLUP``/``CUBE`` of a subclass.

    Args:
        terms: The grouped terms.
    """

    structure_attributes: ClassVar[tuple[str, ...]] = ("terms",)

    keyword: ClassVar[str]

    def __init__(self, *terms: Term) -> None:
        super().__init__()
        self.terms = list(terms)

    def get_children(self) -> Iterator[Node]:
        yield from self.terms

    def get_sql(self, sql_context: SqlContext) -> str:
        return f"{self.keyword}({','.join(term.get_sql(sql_context) for term in self.terms)})"
