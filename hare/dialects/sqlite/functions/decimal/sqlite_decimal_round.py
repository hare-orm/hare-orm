from __future__ import annotations

import decimal
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_DECIMAL_ROUND_FUNCTION_NAME
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions


class SqliteDecimalRound:
    """Backs ``SQLITE_DECIMAL_ROUND_FUNCTION_NAME`` - ``Round()`` of a Decimal, kept as text on SQLite,
    rounded exactly from that text, a tie away from zero as SQLite's own ``ROUND()`` rounds a double."""

    @staticmethod
    def round_decimal(value: Any, scale: int) -> str | None:
        """The text of a decimal rounded half away from zero to exactly ``scale`` places.

        Args:
            value: The number, or None - a float is taken at its shortest round-tripping digits.
            scale: Digits after the decimal point.

        Returns:
            The text, e.g. ``1.10``; None for None.
        """
        if value is None:
            return None
        decimal_value = Decimal(repr(value)) if isinstance(value, float) else Decimal(str(value))
        # A context holding every digit of the result - the default one holds 28.
        context = decimal.Context(prec=max(decimal_value.adjusted() + 1, 1) + max(scale, 0) + 1)
        rounded_value = decimal_value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP, context=context)
        if rounded_value.is_zero():
            rounded_value = abs(rounded_value)
        return format(rounded_value, "f")

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers ``round_decimal`` on ``connection``.

        Args:
            connection: The SQLite connection to register the function on.
        """
        native_functions = SqliteNativeFunctions.module
        round_decimal: Any = cls.round_decimal
        if native_functions is not None:
            # A connection's thread starts with the default context.
            round_decimal = native_functions.DecimalRound(cls.round_decimal, decimal.DefaultContext.prec).round_decimal
        await connection.create_function(SQLITE_DECIMAL_ROUND_FUNCTION_NAME, 2, round_decimal, deterministic=True)
