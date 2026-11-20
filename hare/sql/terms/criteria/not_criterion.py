from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term

T = TypeVar("T")


class Not(Criterion):
    """``NOT condition``."""

    structure_attributes = ("term",)

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__(alias=alias)
        self.term = term

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return self.term.is_aggregate

    def get_children(self) -> Iterator[Node]:
        yield self.term

    def get_sql(self, sql_context: SqlContext) -> str:
        not_context = sql_context.copy(subcriterion=True)
        if sql_context.wraps_negated_criteria:
            sql = f"NOT ({self.term.get_sql(not_context)})"
        else:
            sql = f"NOT {self.term.get_sql(not_context)}"
        return sql_context.format_alias_sql(sql, self.alias)

    @BuilderMethods.ignore_copy
    def __getattr__(self, name: str) -> Any:
        """
        Delegate method calls to the class wrapped by Not().
        Re-wrap methods on child classes of Term (e.g. isin, eg...) to retain 'NOT <term>' output.
        """
        item_function: Callable[..., T] = getattr(self.term, name)

        if not inspect.ismethod(item_function):
            return item_function

        # item_function is bound already - called with the caller's arguments only.
        def inner(*args: Any, **kwargs: Any) -> Not | T:
            result = item_function(*args, **kwargs)
            if isinstance(result, (Term,)):
                return Not(result)
            return result

        return inner
