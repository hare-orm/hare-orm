import math
import statistics
from decimal import Decimal
from typing import Any, ClassVar


class SqliteStatistic:
    """A variance or standard deviation aggregate - also a window function - for SQLite, which has
    none: population or sample, NULLs skipped, NULL without enough values (none for a population,
    fewer than two for a sample), as on Postgres."""

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

    def value(self) -> float | None:
        """The statistic of the values so far.

        Returns:
            The variance or standard deviation, or ``None`` without enough values.
        """
        return self.get_statistic(self.values, self.sample, self.is_deviation)

    @staticmethod
    def get_statistic(values: list[Any], sample: bool, is_deviation: bool) -> float | None:
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
            numbers: list[Any] = [
                Decimal(number) if not isinstance(number, float) else Decimal(repr(number)) for number in values
            ]
        else:
            numbers = [float(number) for number in values]
        variance = statistics.variance(numbers) if sample else statistics.pvariance(numbers)
        if not is_deviation:
            return float(variance)
        return float(variance.sqrt()) if isinstance(variance, Decimal) else math.sqrt(variance)

    def finalize(self) -> float | None:
        """The statistic of every value."""
        return self.value()


class SqliteVarPop(SqliteStatistic):
    pass
