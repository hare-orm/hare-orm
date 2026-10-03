from __future__ import annotations

from collections.abc import Iterator, Sequence
from copy import copy
from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.terms.base.node import TNode
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.table import Table
from hare.sql.terms import (  # noqa: E402
    star as star_terms,
)
from hare.sql.terms.base.term import Term


class SelectReference(Term):
    """A GROUP BY entry naming a selected term by its SELECT position (``GROUP BY 2``).

    Rendering a parameterized expression twice binds its literals twice (``"salary"+$1`` in
    SELECT, ``"salary"+$2`` in GROUP BY), which Postgres no longer matches as the same expression.
    Falls back to the term's own SQL once the term is no longer selected.
    """

    def __init__(self, term: Term) -> None:
        super().__init__()
        self.term = term

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.term.nodes_()

    def get_select_position(self, selects: Sequence[Term]) -> int | None:
        """The 1-based position of the referenced term among `selects`.

        Args:
            selects: The query's SELECT terms.

        Returns:
            The position, or None when the term isn't selected or a ``*`` makes positions unknown.
        """
        if any(isinstance(select_term, star_terms.Star) for select_term in selects):
            return None
        for position, select_term in enumerate(selects, start=1):
            if select_term is self.term:
                return position
        if self.term.alias is not None:
            positions = [
                position
                for position, select_term in enumerate(selects, start=1)
                if select_term.alias == self.term.alias
            ]
            if len(positions) == 1:
                return positions[0]
        return None

    @builder
    def replace_table(  # type:ignore[return]
        self, current_table: Table | None, new_table: Table | None
    ) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the term with the tables replaced.
        """
        self.term = self.term.replace_table(current_table, new_table)

    def get_sql(self, ctx: SqlContext) -> str:
        aliasless_term = self.term
        if aliasless_term.alias is not None:
            aliasless_term = copy(aliasless_term)
            aliasless_term.alias = None
        return aliasless_term.get_sql(ctx.copy(with_alias=False))
