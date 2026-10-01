from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


class NumberText:
    """The text a number is concatenated as - Postgres's own ``::text`` output, reproduced in
    Python for SQLite and for literals."""

    @staticmethod
    def format_float(value: float) -> str:
        """Postgres's text of a double: the shortest round-tripping digits, positional for a
        decimal exponent from -4 to 14, else scientific with a two-digit exponent.

        Args:
            value: The number.

        Returns:
            The text, e.g. ``2``, ``2.25``, ``1e+15``, ``1e-05``.
        """
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == 0:
            return "-0" if math.copysign(1.0, value) < 0 else "0"
        shortest_value = Decimal(repr(value)).normalize()
        sign, digits, exponent = shortest_value.as_tuple()
        decimal_exponent = len(digits) + int(exponent) - 1
        if -4 <= decimal_exponent < 15:
            return format(shortest_value, "f")
        digit_text = "".join(map(str, digits))
        mantissa = digit_text[0] + (f".{digit_text[1:]}" if len(digit_text) > 1 else "")
        exponent_sign = "-" if decimal_exponent < 0 else "+"
        return f"{'-' if sign else ''}{mantissa}e{exponent_sign}{abs(decimal_exponent):02d}"

    @staticmethod
    def format_decimal(value: Any, scale: int) -> str:
        """Text of a decimal rounded half away from zero to exactly ``scale`` places, like
        Postgres's ``ROUND(numeric, scale)::text``.

        Args:
            value: The number - a float is taken at its shortest round-tripping digits.
            scale: Digits after the decimal point.

        Returns:
            The text, e.g. ``1.10``.
        """
        decimal_value = Decimal(repr(value)) if isinstance(value, float) else Decimal(str(value))
        rounded_value = decimal_value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
        if rounded_value.is_zero():
            rounded_value = abs(rounded_value)
        return format(rounded_value, "f")

    @classmethod
    def format_number(cls, value: Any, scale: int | None) -> str | None:
        """Backs ``SQLITE_NUMBER_TEXT_FUNCTION_NAME``.

        Args:
            value: The number, or None.
            scale: Decimal places for a decimal, None for a double.

        Returns:
            The text, or None for None.
        """
        if value is None:
            return None
        if scale is None:
            return cls.format_float(float(value))
        return cls.format_decimal(value, scale)
