from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import TNode

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table
from hare.sql.terms.criteria.criterion import Criterion


class IsNotTrueCriterion(Criterion):
    """``(<term>) IS NOT TRUE`` - FALSE only when ``term`` is TRUE, TRUE when it is FALSE or NULL. The
    term is always parenthesized; a dialect without ``IS NOT TRUE`` writes its own form.
    """

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__(alias=alias)
        self.term = term

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.term.nodes_()

    def get_sql(self, sql_context: SqlContext) -> str:
        renderer = sql_context.dialect.renderers.get(type(self))
        sql = (
            renderer(self, sql_context) if renderer is not None else f"({self.term.get_sql(sql_context)}) IS NOT TRUE"
        )
        return sql_context.format_alias_sql(sql, self.alias)

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the criterion with the tables replaced.
        """
        self.term = self.term.replace_table(current_table, new_table)
        return self
