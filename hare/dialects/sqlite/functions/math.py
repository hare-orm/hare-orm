import math
from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_INTEGER_MAX, SQLITE_INTEGER_MIN, SQLITE_MATH_FUNCTION_PREFIX
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions


class SqliteMathFunctions:
    """The math UDFs ``MathFunction`` renders calls to on SQLite, matching Postgres: an integer
    stays an integer where Postgres keeps one, ``MOD`` takes the dividend's sign, and an argument
    outside a function's domain raises instead of giving NaN."""

    @staticmethod
    def get_number(value: int | float | str | bytes) -> int | float | Decimal:
        """A column value as a number - a DecimalField's stored text as a Decimal.

        Raises:
            ValueError: The value isn't a number.
        """
        if isinstance(value, (int, float)):
            return value
        text = value.decode() if isinstance(value, bytes) else value
        return Decimal(text)

    @staticmethod
    def get_integer_result(value: int | Decimal) -> int | float:
        """A whole number as SQLite can hold it - an integer, or a REAL outside 64 bits."""
        whole_number = int(value)
        if SQLITE_INTEGER_MIN <= whole_number <= SQLITE_INTEGER_MAX:
            return whole_number
        return float(whole_number)

    @classmethod
    def get_float(cls, value: Any) -> float:
        """A number as a float."""
        return float(cls.get_number(value))

    @classmethod
    def absolute(cls, value: Any) -> int | float | None:
        """``ABS`` - an integer of an integer."""
        if value is None:
            return None
        number = cls.get_number(value)
        if isinstance(number, int):
            return abs(number)
        return abs(float(number))

    @classmethod
    def round_to_integral(cls, value: Any, rounding: str) -> int | float | None:
        """``CEIL``/``FLOOR`` - an integer of an integer, a float of a float."""
        if value is None:
            return None
        number = cls.get_number(value)
        if isinstance(number, int):
            return number
        if isinstance(number, float):
            if not math.isfinite(number):
                return number
            return float(math.ceil(number) if rounding == ROUND_CEILING else math.floor(number))
        return cls.get_integer_result(number.to_integral_value(rounding=rounding))

    @classmethod
    def ceiling(cls, value: Any) -> int | float | None:
        """``CEIL``."""
        return cls.round_to_integral(value, ROUND_CEILING)

    @classmethod
    def floor(cls, value: Any) -> int | float | None:
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
    def remainder(cls, dividend: Any, divisor: Any) -> int | float | None:
        """``MOD`` - the dividend's sign, as Postgres.

        Raises:
            ZeroDivisionError: The divisor is zero.
        """
        if dividend is None or divisor is None:
            return None
        left, right = cls.get_number(dividend), cls.get_number(divisor)
        if right == 0:
            raise ZeroDivisionError("division by zero")
        if isinstance(left, int) and isinstance(right, int):
            result = abs(left) % abs(right)
            return result if left >= 0 else -result
        if isinstance(left, float) or isinstance(right, float):
            return math.fmod(float(left), float(right))
        return float(Decimal(left) % Decimal(right))

    @classmethod
    def power(cls, base: Any, exponent: Any) -> float | None:
        """``POWER``.

        Raises:
            ValueError: A negative base with a fractional exponent.
        """
        if base is None or exponent is None:
            return None
        return math.pow(cls.get_float(base), cls.get_float(exponent))

    @classmethod
    def logarithm(cls, base: Any, value: Any) -> float | None:
        """``LOG(base, value)``.

        Raises:
            ValueError: The value or base isn't positive, or the base is 1.
        """
        if base is None or value is None:
            return None
        base_number, number = cls.get_float(base), cls.get_float(value)
        if base_number == 1:
            raise ValueError("division by zero")
        return math.log(number) / math.log(base_number)

    @classmethod
    def cotangent(cls, value: Any) -> float | None:
        """``COT`` - infinity at a zero tangent, as Postgres."""
        if value is None:
            return None
        tangent = math.tan(cls.get_float(value))
        return math.inf if tangent == 0 else 1 / tangent

    @classmethod
    def get_unary(cls, function: Callable[[float], float]) -> Callable[[Any], float | None]:
        """A one-argument float function that passes NULL through."""

        def call(value: Any) -> float | None:
            if value is None:
                return None
            return function(cls.get_float(value))

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
            "exp": (1, cls.get_unary(math.exp)),
            "ln": (1, cls.get_unary(math.log)),
            "sin": (1, cls.get_unary(math.sin)),
            "cos": (1, cls.get_unary(math.cos)),
            "tan": (1, cls.get_unary(math.tan)),
            "cot": (1, cls.cotangent),
            "asin": (1, cls.get_unary(math.asin)),
            "acos": (1, cls.get_unary(math.acos)),
            "atan": (1, cls.get_unary(math.atan)),
            "atan2": (
                2,
                lambda y, x: None if y is None or x is None else math.atan2(cls.get_float(y), cls.get_float(x)),
            ),
            "degrees": (1, cls.get_unary(math.degrees)),
            "radians": (1, cls.get_unary(math.radians)),
            "pi": (0, lambda: math.pi),
        }

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers every math UDF on ``connection``."""
        functions = cls.get_functions()
        native_functions = SqliteNativeFunctions.module
        native_math = None if native_functions is None else native_functions.MathFunctions(functions)
        for name, (argument_count, function) in functions.items():
            if native_math is not None:
                function = getattr(native_math, name)
            await connection.create_function(
                f"{SQLITE_MATH_FUNCTION_PREFIX}{name}", argument_count, function, deterministic=True
            )
