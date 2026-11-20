from __future__ import annotations

import math
import statistics
from typing import Any, ClassVar


class SqliteStatistic:
    """A variance or standard deviation aggregate - also a window function - for SQLite, which has
    none: population or sample, NULLs skipped, NULL without enough values (none for a population,
    fewer than two for a sample), as hare's statistic functions are. ``rust.native.sqlite_functions``
    runs it as an aggregate; the statistic itself is computed here."""

    #: Whether the value is the sample statistic rather than the population one.
    sample: ClassVar[bool] = False
    #: Whether the value is the standard deviation rather than the variance.
    is_deviation: ClassVar[bool] = False

    @staticmethod
    def get_statistic(values: list[Any], sample: bool, is_deviation: bool) -> float | None:
        """The variance or standard deviation of numbers.

        Args:
            values: The numbers - ints and floats.
            sample: The sample statistic rather than the population one.
            is_deviation: The standard deviation rather than the variance.

        Returns:
            The statistic, or ``None`` without enough values.
        """
        if len(values) < (2 if sample else 1):
            return None
        numbers = [float(number) for number in values]
        variance = statistics.variance(numbers) if sample else statistics.pvariance(numbers)
        if not is_deviation:
            return float(variance)
        return math.sqrt(variance)
