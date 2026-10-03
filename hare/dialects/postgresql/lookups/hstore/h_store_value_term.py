from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.context import SqlContext
from hare.sql.functions.cast import Cast
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:  # pragma: nocoverage
    pass


class HStoreValueTerm(Function):
    """``hstore_column -> key`` - the value of one key, NULL when it's missing."""

    def __init__(self, term: Term, key: str) -> None:
        super().__init__("hstore_value", term, Cast(ValueWrapper(key), "text"))

    def get_function_sql(self, ctx: SqlContext) -> str:
        column, key = self.args
        return f"({self.get_arg_sql(column, ctx)} -> {self.get_arg_sql(key, ctx)})"
