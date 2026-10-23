from __future__ import annotations

import math
from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_INTEGER_MAX, SQLITE_INTEGER_MIN, SQLITE_MATH_FUNCTION_PREFIX
from hare.dialects.sqlite.functions.decimal.sqlite_decimal_arithmetic import SqliteDecimalArithmetic
from hare.dialects.sqlite.functions.sqlite_function_errors import SqliteFunctionErrors
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions


class SqliteMathFunctions:
    """The math UDFs ``MathFunction`` renders calls to on SQLite - SQLite's own math functions are an
    optional build feature. Each gives what SQLite's own does: NULL outside the function's domain, an
    infinity past a float, an integer of an integer where SQLite keeps one; a ``DecimalField``'s
    stored text is worked out exactly."""

    @staticmethod
    def get_number(value: int | float | str | bytes) -> int | float | Decimal:
        """A column value as a number - a DecimalField's stored text as a Decimal.

        Raises:
            decimal.InvalidOperation: The stored text isn't a number.
        """
        if isinstance(value, (int, float)):
            return value
        text = value.decode() if isinstance(value, bytes) else value
        return Decimal(text)

    @staticmethod
    def get_integer_result(value: int | Decimal) -> int | str:
        """A whole number as SQLite can hold it - an integer, or its exact text outside 64 bits, as a
        Decimal is stored."""
        whole_number = int(value)
        if SQLITE_INTEGER_MIN <= whole_number <= SQLITE_INTEGER_MAX:
            return whole_number
        return str(whole_number)

    @classmethod
    def get_float(cls, value: Any) -> float:
        """A number as a float."""
        return float(cls.get_number(value))

    @classmethod
    def absolute(cls, value: Any) -> int | float | str | None:
        """``ABS`` - an integer of an integer, the exact text of a Decimal."""
        if value is None:
            return None
        number = cls.get_number(value)
        if isinstance(number, int):
            return abs(number)
        if isinstance(number, Decimal):
            # copy_abs() - abs() rounds to the context's 28 digits.
            return SqliteDecimalArithmetic.get_text(number.copy_abs())
        return abs(number)

    @classmethod
    def round_to_integral(cls, value: Any, rounding: str) -> int | float | str | None:
        """``CEIL``/``FLOOR`` - an integer of an integer, a float of a float."""
        if value is None:
            return None
        number = cls.get_number(value)
        if isinstance(number, int):
            return number
        if isinstance(number, float):
            if not math.isfinite(number):
                return number
            # math.ceil() goes through an int - the sign of a zero, as C's ceil() keeps it, is put back.
            return math.copysign(float(math.ceil(number) if rounding == ROUND_CEILING else math.floor(number)), number)
        return cls.get_integer_result(number.to_integral_value(rounding=rounding))

    @classmethod
    def ceiling(cls, value: Any) -> int | float | str | None:
        """``CEIL``."""
        return cls.round_to_integral(value, ROUND_CEILING)

    @classmethod
    def floor(cls, value: Any) -> int | float | str | None:
        """``FLOOR``."""
        return cls.round_to_integral(value, ROUND_FLOOR)

    @classmethod
    def sign(cls, value: Any) -> int | None:
        """``SIGN`` - -1, 0 or 1."""
        if value is None:
            return None
        number = cls.get_number(value)
        return (number > 0) - (number < 0)

    @classmethod
    def remainder(cls, dividend: Any, divisor: Any) -> int | float | str | None:
        """``MOD`` - the dividend's sign, as SQLite's ``%``; NULL of a zero divisor."""
        if dividend is None or divisor is None:
            return None
        left, right = cls.get_number(dividend), cls.get_number(divisor)
        if right == 0:
            return None
        if isinstance(left, int) and isinstance(right, int):
            result = abs(left) % abs(right)
            return result if left >= 0 else -result
        if isinstance(left, float) or isinstance(right, float):
            return math.fmod(float(left), float(right))
        remainder = SqliteDecimalArithmetic.remainder_numbers(Decimal(left), Decimal(right))
        return None if remainder is None else SqliteDecimalArithmetic.get_text(remainder)

    @classmethod
    def power(cls, base: Any, exponent: Any) -> float | None:
        """``POWER`` - NULL of a negative base to a fractional power and of zero to a negative one."""
        if base is None or exponent is None:
            return None
        base_number, exponent_number = cls.get_float(base), cls.get_float(exponent)
        try:
            return math.pow(base_number, exponent_number)
        except OverflowError:
            negative = base_number < 0 and exponent_number.is_integer() and exponent_number % 2 == 1
            return -math.inf if negative else math.inf
        except ValueError:
            return None

    @classmethod
    def exponential(cls, value: Any) -> float | None:
        """``EXP``."""
        if value is None:
            return None
        try:
            return math.exp(cls.get_float(value))
        except OverflowError:
            return math.inf

    @classmethod
    def logarithm(cls, base: Any, value: Any) -> float | None:
        """``LOG(base, value)`` - NULL of a value or a base that isn't positive, and of a base of 1."""
        if base is None or value is None:
            return None
        base_number, number = cls.get_float(base), cls.get_float(value)
        if base_number <= 0 or number <= 0 or base_number == 1:
            return None
        return math.log(number) / math.log(base_number)

    @classmethod
    def natural_logarithm(cls, value: Any) -> float | None:
        """``LN`` - NULL of a value that isn't positive."""
        if value is None:
            return None
        number = cls.get_float(value)
        return math.log(number) if number > 0 else None

    @classmethod
    def cotangent(cls, value: Any) -> float | None:
        """``COT`` - NULL at a zero tangent and of an infinity."""
        if value is None:
            return None
        try:
            tangent = math.tan(cls.get_float(value))
        except ValueError:
            return None
        return None if tangent == 0 else 1 / tangent

    @classmethod
    def get_unary(cls, function: Callable[[float], float]) -> Callable[[Any], float | None]:
        """A one-argument float function that passes NULL through - NULL outside its domain, an
        infinity past a float."""

        def call(value: Any) -> float | None:
            if value is None:
                return None
            try:
                return function(cls.get_float(value))
            except OverflowError:
                return math.inf
            except ValueError:
                return None

        return call

    @classmethod
    def get_functions(cls) -> dict[str, tuple[int, Callable[..., Any]]]:
        """Every UDF by its name after the prefix, with its argument count."""
        return {
            "abs": (1, cls.absolute),
            "ceil": (1, cls.ceiling),
            "floor": (1, cls.floor),
            "sign": (1, cls.sign),
            "mod": (2, cls.remainder),
            "power": (2, cls.power),
            "log": (2, cls.logarithm),
            "sqrt": (1, cls.get_unary(math.sqrt)),
            "exp": (1, cls.exponential),
            "ln": (1, cls.natural_logarithm),
            "sin": (1, cls.get_unary(math.sin)),
            "cos": (1, cls.get_unary(math.cos)),
            "tan": (1, cls.get_unary(math.tan)),
            "cot": (1, cls.cotangent),
            "asin": (1, cls.get_unary(math.asin)),
            "acos": (1, cls.get_unary(math.acos)),
            "atan": (1, cls.get_unary(math.atan)),
            "atan2": (
                2,
                lambda ordinate, abscissa: (
                    None
                    if ordinate is None or abscissa is None
                    else math.atan2(cls.get_float(ordinate), cls.get_float(abscissa))
                ),
            ),
            "degrees": (1, cls.get_unary(math.degrees)),
            "radians": (1, cls.get_unary(math.radians)),
            "pi": (0, lambda: math.pi),
        }

    @classmethod
    async def install(cls, connection: aiosqlite.Connection, function_errors: SqliteFunctionErrors) -> None:
        """Registers every math UDF on ``connection``.

        Args:
            connection: The SQLite connection.
            function_errors: The connection's function errors.
        """
        functions = {
            name: (argument_count, function_errors.wrap(function))
            for name, (argument_count, function) in cls.get_functions().items()
        }
        native_functions = SqliteNativeFunctions.module
        native_math = None if native_functions is None else native_functions.MathFunctions(functions)
        for name, (argument_count, function) in functions.items():
            if native_math is not None:
                function = getattr(native_math, name)
            await connection.create_function(
                f"{SQLITE_MATH_FUNCTION_PREFIX}{name}", argument_count, function, deterministic=True
            )
