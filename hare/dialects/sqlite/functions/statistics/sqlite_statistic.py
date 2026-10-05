from __future__ import annotations

import decimal
import functools
import math
import statistics
from decimal import Decimal
from typing import Any, ClassVar, cast

from hare.sql.constants import DECIMAL_QUOTIENT_SCALE


class SqliteStatistic:
    """A variance or standard deviation aggregate - also a window function - for SQLite, which has
    none: population or sample, NULLs skipped, NULL without enough values (none for a population,
    fewer than two for a sample), as hare's statistic functions are."""

    #: Whether the value is the sample statistic rather than the population one.
    sample: ClassVar[bool] = False
    #: Whether the value is the standard deviation rather than the variance.
    is_deviation: ClassVar[bool] = False

    def __init__(self) -> None:
        self.values: list[Any] = []

    @staticmethod
    def get_number(value: Any) -> int | float | Decimal:
        """A column value as a number - a DecimalField's stored text as a Decimal."""
        if isinstance(value, (int, float)):
            return value
        return Decimal(value.decode() if isinstance(value, bytes) else value)

    def step(self, value: Any) -> None:
        """Adds a row's value."""
        if value is not None:
            self.values.append(self.get_number(value))

    def inverse(self, value: Any) -> None:
        """Removes a row's value leaving the window frame."""
        if value is not None:
            self.values.remove(self.get_number(value))

    def value(self) -> float | str | None:
        """The statistic of the values so far.

        Returns:
            The variance or standard deviation, or ``None`` without enough values.
        """
        return self.get_statistic(self.values, self.sample, self.is_deviation)

    @staticmethod
    def get_statistic(values: list[Any], sample: bool, is_deviation: bool) -> float | str | None:
        """The variance or standard deviation of numbers.

        Args:
            values: The numbers - ints, floats and Decimals.
            sample: The sample statistic rather than the population one.
            is_deviation: The standard deviation rather than the variance.

        Returns:
            The statistic, or ``None`` without enough values.
        """
        if len(values) < (2 if sample else 1):
            return None
        if any(isinstance(number, Decimal) for number in values):
            return SqliteStatistic.get_decimal_statistic(
                [Decimal(number) if not isinstance(number, float) else Decimal(repr(number)) for number in values],
                sample,
                is_deviation,
            )
        numbers = [float(number) for number in values]
        variance = statistics.variance(numbers) if sample else statistics.pvariance(numbers)
        if not is_deviation:
            return float(variance)
        return math.sqrt(variance)

    @staticmethod
    def get_decimal_statistic(numbers: list[Decimal], sample: bool, is_deviation: bool) -> str:
        """The variance or standard deviation of Decimals - the exact sums of the values and of their
        squares, the quotient and its square root cut at ``DECIMAL_QUOTIENT_SCALE`` places.

        Args:
            numbers: The Decimals, enough of them.
            sample: The sample statistic rather than the population one.
            is_deviation: The standard deviation rather than the variance.

        Returns:
            The statistic's text.
        """
        # Local import: the decimal functions import the statistics' package.
        from hare.dialects.sqlite.functions.decimal.sqlite_decimal_arithmetic import SqliteDecimalArithmetic

        context = SqliteDecimalArithmetic.EXACT_CONTEXT
        count = Decimal(len(numbers))
        total = functools.reduce(context.add, numbers, Decimal(0))
        total_of_squares = functools.reduce(
            context.add, (context.multiply(number, number) for number in numbers), Decimal(0)
        )
        numerator = context.subtract(context.multiply(count, total_of_squares), context.multiply(total, total))
        if numerator <= 0:
            return "0"
        denominator = context.multiply(count, count - 1 if sample else count)
        variance = cast("Decimal", SqliteDecimalArithmetic.divide_numbers(numerator, denominator))
        if not is_deviation:
            return SqliteDecimalArithmetic.get_text(variance)
        precision = max(1, variance.adjusted() // 2 + 2 + DECIMAL_QUOTIENT_SCALE)
        root = variance.sqrt(
            context=decimal.Context(
                prec=precision, rounding=decimal.ROUND_DOWN, Emax=decimal.MAX_EMAX, Emin=decimal.MIN_EMIN
            )
        )
        return SqliteDecimalArithmetic.get_text(
            root.quantize(Decimal(1).scaleb(-DECIMAL_QUOTIENT_SCALE), rounding=decimal.ROUND_DOWN, context=context)
        )

    def finalize(self) -> float | str | None:
        """The statistic of every value."""
        return self.value()
