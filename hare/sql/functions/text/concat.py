from __future__ import annotations

from typing import Any

from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function


class Concat(Function):
    """The texts of its terms joined, a NULL term read as empty text - ISO SQL's ``||`` over
    ``COALESCE(term, '')``; a dialect with a ``CONCAT()`` of the same meaning renders that."""

    def __init__(self, *terms: Any, **kwargs: Any) -> None:
        super().__init__("CONCAT", *terms, **kwargs)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        terms_sql = [f"COALESCE({self.get_arg_sql(term, sql_context)}, '')" for term in self.args]
        return f"({' || '.join(terms_sql)})"
