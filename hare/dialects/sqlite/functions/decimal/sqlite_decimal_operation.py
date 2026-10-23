from __future__ import annotations

from typing import Any

from hare.dialects.sqlite.functions.constants import (
    SQLITE_DECIMAL_OPERATION_FUNCTION_NAMES,
    SQLITE_DECIMAL_OPERATION_SQL_OPERATORS,
)
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function


class SqliteDecimalOperation(Function):
    """A sum, difference, product or quotient of Decimals worked out exactly by hare's UDF - SQLite's
    own arithmetic runs on doubles. SQL kept in DDL names no function: it is SQLite's arithmetic
    there."""

    def __init__(self, operator_name: str, left: Any, right: Any, alias: str | None = None) -> None:
        """
        Args:
            operator_name: The arithmetic operator's name - ``add``, ``sub``, ``mul`` or ``truediv``.
            left: The left operand, a Decimal's text or a number.
            right: The right operand.
            alias: The term's alias.
        """
        super().__init__(SQLITE_DECIMAL_OPERATION_FUNCTION_NAMES[operator_name], left, right, alias=alias)
        self.operator_name = operator_name

    def get_function_sql(self, sql_context: SqlContext) -> str:
        left_sql, right_sql = (self.get_arg_sql(argument, sql_context) for argument in self.args)
        if sql_context.native_functions_only:
            return f"({left_sql}{SQLITE_DECIMAL_OPERATION_SQL_OPERATORS[self.operator_name]}{right_sql})"
        return f"{self.name}({left_sql},{right_sql})"
