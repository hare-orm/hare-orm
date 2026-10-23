from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.renderers.constants import CLICKHOUSE_CHECKED_INTEGER_TEMPLATE
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.term import Term


class ClickhouseCheckedIntegerResult(Function):
    """An integer result refused out of its type's range, its term written once - a result of
    arithmetic holding other checked results, which the plain check (``term + throwIf(term < ... OR
    term > ...)``) would write three times each, its SQL tripling with each level.

    Args:
        term: The result.
        minimum: The type's least value.
        maximum: The type's greatest value.
        type_name: The type, as the error names it.
    """

    def __init__(self, term: Term, minimum: int, maximum: int, type_name: str) -> None:
        super().__init__("hare_checked_integer", term)
        self.minimum = minimum
        self.maximum = maximum
        self.type_name = type_name

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return CLICKHOUSE_CHECKED_INTEGER_TEMPLATE.format(
            self.get_arg_sql(self.args[0], sql_context), self.minimum, self.maximum, self.type_name
        )
