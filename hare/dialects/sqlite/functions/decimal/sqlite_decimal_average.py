from __future__ import annotations

from decimal import Decimal

from hare.dialects.sqlite.functions.constants import SQLITE_DECIMAL_AVERAGE_FUNCTION_NAME
from hare.dialects.sqlite.functions.decimal.sqlite_decimal_arithmetic import SqliteDecimalArithmetic
from hare.dialects.sqlite.functions.decimal.sqlite_decimal_sum import SqliteDecimalSum


class SqliteDecimalAverage(SqliteDecimalSum):
    """``AVG`` of Decimals - also a window function: the exact sum divided by the count, cut at
    ``DECIMAL_QUOTIENT_SCALE`` places."""

    function_name = SQLITE_DECIMAL_AVERAGE_FUNCTION_NAME

    def value(self) -> str | None:
        """The average of the values so far.

        Returns:
            Its text, None of no value.
        """
        average = SqliteDecimalArithmetic.divide_numbers(self.total, Decimal(self.count))
        return None if average is None else SqliteDecimalArithmetic.get_text(average)
