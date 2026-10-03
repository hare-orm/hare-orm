from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, TypeVar

from hare.sql.context import SqlContext
from hare.sql.terms.base.node import TNode
from hare.sql.terms.base.term import Term
from hare.sql.utils import builder, ignore_copy

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.table import Table
from hare.sql.terms.criteria.criterion import Criterion

T = TypeVar("T")


class Not(Criterion):
    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__(alias=alias)
        self.term = term

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return self.term.is_aggregate

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.term.nodes_()

    def get_sql(self, ctx: SqlContext) -> str:
        not_ctx = ctx.copy(subcriterion=True)
        sql = f"NOT {self.term.get_sql(not_ctx)}"
        return ctx.format_alias_sql(sql, self.alias)

    @ignore_copy
    def __getattr__(self, name: str) -> Any:
        """
        Delegate method calls to the class wrapped by Not().
        Re-wrap methods on child classes of Term (e.g. isin, eg...) to retain 'NOT <term>' output.
        """
        item_func: Callable[..., T] = getattr(self.term, name)

        if not inspect.ismethod(item_func):
            return item_func

        # item_func is bound already - called with the caller's arguments only.
        def inner(*args: Any, **kwargs: Any) -> Not | T:
            result = item_func(*args, **kwargs)
            if isinstance(result, (Term,)):
                return Not(result)
            return result

        return inner

    @builder
    def replace_table(  # type:ignore[return]
        self, current_table: Table | None, new_table: Table | None
    ) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the criterion with the tables replaced.
        """
        self.term = self.term.replace_table(current_table, new_table)
