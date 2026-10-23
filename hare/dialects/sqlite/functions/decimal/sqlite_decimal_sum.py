from __future__ import annotations

from decimal import Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.functions.constants import SQLITE_DECIMAL_SUM_FUNCTION_NAME
from hare.dialects.sqlite.functions.decimal.sqlite_decimal_arithmetic import SqliteDecimalArithmetic


class SqliteDecimalSum:
    """``SUM`` of Decimals - also a window function - added up exactly, as hare's Decimal sum is:
    NULLs skipped, NULL of no value, the largest scale of the values."""

    #: The name the aggregate is registered under.
    function_name = SQLITE_DECIMAL_SUM_FUNCTION_NAME

    def __init__(self) -> None:
        self.total = Decimal(0)
        self.count = 0

    def step(self, value: Any) -> None:
        """Adds a row's value."""
        number = SqliteDecimalArithmetic.get_decimal(value)
        if number is not None:
            self.total = SqliteDecimalArithmetic.EXACT_CONTEXT.add(self.total, number)
            self.count += 1

    def inverse(self, value: Any) -> None:
        """Removes a row's value leaving the window frame."""
        number = SqliteDecimalArithmetic.get_decimal(value)
        if number is not None:
            self.total = SqliteDecimalArithmetic.EXACT_CONTEXT.subtract(self.total, number)
            self.count -= 1

    def value(self) -> str | None:
        """The sum of the values so far.

        Returns:
            Its text, None of no value.
        """
        if not self.count:
            return None
        return SqliteDecimalArithmetic.get_text(self.total)

    def finalize(self) -> str | None:
        """The sum of every value."""
        return self.value()

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers the aggregate on ``connection`` as a window function, usable as a plain
        aggregate too.

        Args:
            connection: The SQLite connection.
        """
        # aiosqlite wraps create_function() but not create_window_function().
        await connection._execute(  # type: ignore[no-untyped-call]
            connection._conn.create_window_function, cls.function_name, 1, cls
        )
