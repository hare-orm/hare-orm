from __future__ import annotations

from hare.sql.functions.cast import Cast
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class HStoreValueTerm(Function):
    """``hstore_column -> key`` - the value of one key, NULL when it's missing."""

    def __init__(self, term: Term, key: str) -> None:
        super().__init__("hstore_value", term, Cast(ValueWrapper(key), "text"))

    def get_function_sql(self, sql_context: SqlContext) -> str:
        column, key = self.args
        return f"({self.get_arg_sql(column, sql_context)} -> {self.get_arg_sql(key, sql_context)})"
