from __future__ import annotations

import math
from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_INTEGER_MAX, SQLITE_INTEGER_MIN, SQLITE_MATH_FUNCTION_PREFIX
from hare.dialects.sqlite.functions.constants import (
    FLOAT_OVERFLOW_MESSAGE,
    FLOAT_UNDERFLOW_MESSAGE,
    LOGARITHM_OF_NEGATIVE_MESSAGE,
    LOGARITHM_OF_ZERO_MESSAGE,
    MATH_DOMAIN_MESSAGES,
    MATH_INPUT_OUT_OF_RANGE_MESSAGE,
    NEGATIVE_FRACTIONAL_POWER_MESSAGE,
    ZERO_NEGATIVE_POWER_MESSAGE,
)
from hare.dialects.sqlite.functions.sqlite_function_errors import SqliteFunctionErrors
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions


class SqliteMathFunctions:
    """The math UDFs ``MathFunction`` renders calls to on SQLite, matching Postgres: an integer
    stays an integer where Postgres keeps one, ``MOD`` takes the dividend's sign, and an argument
    outside a function's domain raises instead of giving NaN, with Postgres's message. Every error is
    a ``ValueError`` - ``sqlite3`` reports an ``OverflowError`` as a too long text."""

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
            # math.ceil() goes through an int - the sign of a zero, as PostgreSQL keeps it, is put back.
            return math.copysign(float(math.ceil(number) if rounding == ROUND_CEILING else math.floor(number)), number)
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
            ValueError: A negative base with a fractional exponent, zero to a negative power, or a
                result beyond a float.
        """
        if base is None or exponent is None:
            return None
        base_number, exponent_number = cls.get_float(base), cls.get_float(exponent)
        try:
            result = math.pow(base_number, exponent_number)
        except OverflowError:
            raise ValueError(FLOAT_OVERFLOW_MESSAGE) from None
        except ValueError:
            message = ZERO_NEGATIVE_POWER_MESSAGE if base_number == 0 else NEGATIVE_FRACTIONAL_POWER_MESSAGE
            raise ValueError(message) from None
        if result == 0 and base_number != 0 and math.isfinite(base_number) and math.isfinite(exponent_number):
            raise ValueError(FLOAT_UNDERFLOW_MESSAGE)
        return result

    @classmethod
    def exponential(cls, value: Any) -> float | None:
        """``EXP``.

        Raises:
            ValueError: A result beyond a float.
        """
        if value is None:
            return None
        number = cls.get_float(value)
        try:
            result = math.exp(number)
        except OverflowError:
            raise ValueError(FLOAT_OVERFLOW_MESSAGE) from None
        if result == 0 and math.isfinite(number):
            raise ValueError(FLOAT_UNDERFLOW_MESSAGE)
        return result

    @classmethod
    def divisor(cls, dividend: Any, divisor: Any) -> Any:
        """The divisor of a division, as it is.

        Raises:
            ZeroDivisionError: The divisor is zero and the dividend isn't NULL - as PostgreSQL's division.
        """
        if dividend is not None and divisor is not None and cls.get_number(divisor) == 0:
            raise ZeroDivisionError("division by zero")
        return divisor

    @classmethod
    def logarithm(cls, base: Any, value: Any) -> float | None:
        """``LOG(base, value)``.

        Raises:
            ValueError: The value or base isn't positive, or the base is 1.
        """
        if base is None or value is None:
            return None
        base_number, number = cls.get_float(base), cls.get_float(value)
        for logarithm_argument in (base_number, number):
            cls.raise_if_logarithm_undefined(logarithm_argument)
        if base_number == 1:
            raise ValueError("division by zero")
        return math.log(number) / math.log(base_number)

    @staticmethod
    def raise_if_logarithm_undefined(number: float) -> None:
        """Refuses the logarithm of zero or a negative number, with Postgres's message.

        Raises:
            ValueError: The number isn't positive.
        """
        if number == 0:
            raise ValueError(LOGARITHM_OF_ZERO_MESSAGE)
        if number < 0:
            raise ValueError(LOGARITHM_OF_NEGATIVE_MESSAGE)

    @classmethod
    def natural_logarithm(cls, value: Any) -> float | None:
        """``LN``.

        Raises:
            ValueError: The value isn't positive.
        """
        if value is None:
            return None
        number = cls.get_float(value)
        cls.raise_if_logarithm_undefined(number)
        return math.log(number)

    @classmethod
    def cotangent(cls, value: Any) -> float | None:
        """``COT`` - infinity at a zero tangent, as Postgres."""
        if value is None:
            return None
        try:
            tangent = math.tan(cls.get_float(value))
        except ValueError:
            raise ValueError(MATH_INPUT_OUT_OF_RANGE_MESSAGE) from None
        return math.inf if tangent == 0 else 1 / tangent

    @classmethod
    def get_unary(cls, name: str, function: Callable[[float], float]) -> Callable[[Any], float | None]:
        """A one-argument float function that passes NULL through, its errors Postgres's."""
        domain_message = MATH_DOMAIN_MESSAGES.get(name, MATH_INPUT_OUT_OF_RANGE_MESSAGE)

        def call(value: Any) -> float | None:
            if value is None:
                return None
            try:
                return function(cls.get_float(value))
            except OverflowError:
                raise ValueError(FLOAT_OVERFLOW_MESSAGE) from None
            except ValueError:
                raise ValueError(domain_message) from None

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
            "divisor": (2, cls.divisor),
            "log": (2, cls.logarithm),
            "sqrt": (1, cls.get_unary("sqrt", math.sqrt)),
            "exp": (1, cls.exponential),
            "ln": (1, cls.natural_logarithm),
            "sin": (1, cls.get_unary("sin", math.sin)),
            "cos": (1, cls.get_unary("cos", math.cos)),
            "tan": (1, cls.get_unary("tan", math.tan)),
            "cot": (1, cls.cotangent),
            "asin": (1, cls.get_unary("asin", math.asin)),
            "acos": (1, cls.get_unary("acos", math.acos)),
            "atan": (1, cls.get_unary("atan", math.atan)),
            "atan2": (
                2,
                lambda ordinate, abscissa: (
                    None
                    if ordinate is None or abscissa is None
                    else math.atan2(cls.get_float(ordinate), cls.get_float(abscissa))
                ),
            ),
            "degrees": (1, cls.get_unary("degrees", math.degrees)),
            "radians": (1, cls.get_unary("radians", math.radians)),
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
