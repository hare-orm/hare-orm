from __future__ import annotations

import decimal
from collections.abc import Callable
from decimal import ROUND_DOWN, Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.functions.constants import (
    SQLITE_DECIMAL_MOD_FUNCTION_NAME,
    SQLITE_DECIMAL_OPERATION_FUNCTION_NAMES,
)
from hare.dialects.sqlite.functions.sqlite_function_errors import SqliteFunctionErrors
from hare.sql.constants import DECIMAL_QUOTIENT_SCALE


class SqliteDecimalArithmetic:
    """The arithmetic of Decimals on SQLite - SQLite's own runs on doubles, which keep ~15 significant
    digits: a sum, difference, product and remainder exact, a quotient cut at ``DECIMAL_QUOTIENT_SCALE``
    places. A zero divisor gives NULL, as SQLite's own division does. Each UDF takes a
    ``DecimalField``'s stored text, an integer or a float, and gives the result's text."""

    #: Exact arithmetic - no sum, difference or product is rounded.
    EXACT_CONTEXT = decimal.Context(
        prec=decimal.MAX_PREC,
        Emax=decimal.MAX_EMAX,
        Emin=decimal.MIN_EMIN,
        traps=[decimal.InvalidOperation, decimal.Overflow],
    )

    @staticmethod
    def get_decimal(value: int | float | str | bytes | None) -> Decimal | None:
        """A SQLite value as a Decimal.

        Args:
            value: The value - a ``DecimalField``'s text, an integer, a float or NULL.

        Returns:
            The Decimal, None for NULL.

        Raises:
            decimal.InvalidOperation: The text is no number.
        """
        if value is None:
            return None
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, float):
            return Decimal(repr(value))
        return Decimal(value.decode() if isinstance(value, bytes) else value)

    @staticmethod
    def get_text(number: Decimal) -> str:
        """A Decimal's text in fixed-point - the form a ``DecimalField`` stores.

        Args:
            number: The Decimal.

        Returns:
            The text.
        """
        return format(number, "f")

    @classmethod
    def divide_numbers(cls, dividend: Decimal, divisor: Decimal) -> Decimal | None:
        """The quotient of two Decimals cut at ``DECIMAL_QUOTIENT_SCALE`` places.

        Args:
            dividend: The dividend.
            divisor: The divisor.

        Returns:
            The quotient, None of a zero divisor.
        """
        if not divisor:
            return None
        precision = max(1, dividend.adjusted() - divisor.adjusted() + 2 + DECIMAL_QUOTIENT_SCALE)
        quotient = decimal.Context(
            prec=precision, rounding=ROUND_DOWN, Emax=decimal.MAX_EMAX, Emin=decimal.MIN_EMIN
        ).divide(dividend, divisor)
        return quotient.quantize(
            Decimal(1).scaleb(-DECIMAL_QUOTIENT_SCALE), rounding=ROUND_DOWN, context=cls.EXACT_CONTEXT
        )

    @classmethod
    def remainder_numbers(cls, dividend: Decimal, divisor: Decimal) -> Decimal | None:
        """The remainder of two Decimals - the dividend's sign.

        Args:
            dividend: The dividend.
            divisor: The divisor.

        Returns:
            The remainder, None of a zero divisor.
        """
        if not divisor:
            return None
        return cls.EXACT_CONTEXT.remainder(dividend, divisor)

    @classmethod
    def get_operation(
        cls, operation: Callable[[Decimal, Decimal], Decimal | None]
    ) -> Callable[[Any, Any], str | None]:
        """A UDF working out ``operation`` of two SQLite values.

        Args:
            operation: The operation of two Decimals.

        Returns:
            The UDF - NULL of a NULL operand or a NULL result, else the result's text.
        """

        def calculate(left: Any, right: Any) -> str | None:
            left_number, right_number = cls.get_decimal(left), cls.get_decimal(right)
            if left_number is None or right_number is None:
                return None
            result = operation(left_number, right_number)
            return None if result is None else cls.get_text(result)

        return calculate

    @classmethod
    def get_functions(cls) -> dict[str, Callable[[Any, Any], str | None]]:
        """Every UDF by its name.

        Returns:
            The UDFs.
        """
        operations: dict[str, Callable[[Decimal, Decimal], Decimal | None]] = {
            "add": cls.EXACT_CONTEXT.add,
            "sub": cls.EXACT_CONTEXT.subtract,
            "mul": cls.EXACT_CONTEXT.multiply,
            "truediv": cls.divide_numbers,
        }
        functions = {
            SQLITE_DECIMAL_OPERATION_FUNCTION_NAMES[name]: cls.get_operation(operation)
            for name, operation in operations.items()
        }
        functions[SQLITE_DECIMAL_MOD_FUNCTION_NAME] = cls.get_operation(cls.remainder_numbers)
        return functions

    @classmethod
    async def install(cls, connection: aiosqlite.Connection, function_errors: SqliteFunctionErrors) -> None:
        """Registers every Decimal arithmetic UDF on ``connection``.

        Args:
            connection: The SQLite connection.
            function_errors: The connection's function errors.
        """
        for name, function in cls.get_functions().items():
            await connection.create_function(name, 2, function_errors.wrap(function), deterministic=True)
