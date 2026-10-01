from __future__ import annotations

from typing import TYPE_CHECKING, cast

from hare.sql.context import SqlContext
from hare.sql.exceptions import QueryException

if TYPE_CHECKING:
    pass
from hare.sql.terms.base.term import Term


class Parameter(Term):
    """
    Represents a parameter in a query. The placeholder can be specified with the `placeholder` argument or
    will be determined based on the dialect if not provided.
    """

    is_aggregate = None

    def __init__(self, placeholder: str | None = None, idx: int | None = None) -> None:
        if not placeholder and idx is None:
            raise QueryException("Must provide either a placeholder or an idx")

        if idx is not None and idx < 1:
            raise QueryException("idx must start at 1")

        if placeholder and idx:
            raise QueryException("Cannot provide both a placeholder and an idx")

        self._placeholder = placeholder
        self._idx = idx

    def get_sql(self, ctx: SqlContext) -> str:
        if self._placeholder:
            return self._placeholder

        return ctx.dialect.get_placeholder(cast("int", self._idx))
