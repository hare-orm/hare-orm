from __future__ import annotations

from typing import cast

from hare.sql.exceptions import QueryException
from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term


class Parameter(Term):
    """
    Represents a parameter in a query. The placeholder can be specified with the `placeholder` argument or
    will be determined based on the dialect if not provided.
    """

    is_aggregate = None

    def __init__(self, placeholder: str | None = None, index: int | None = None) -> None:
        if not placeholder and index is None:
            raise QueryException("Must provide either a placeholder or an idx")

        if index is not None and index < 1:
            raise QueryException("idx must start at 1")

        if placeholder and index:
            raise QueryException("Cannot provide both a placeholder and an idx")

        self._placeholder = placeholder
        self._index = index

    def get_sql(self, sql_context: SqlContext) -> str:
        if self._placeholder:
            return self._placeholder

        return sql_context.dialect.parameters.get_placeholder(cast("int", self._index))
