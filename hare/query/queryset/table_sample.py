from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hare.exceptions import QueryError
from hare.numbers.finite_numbers import FiniteNumbers
from hare.query.enums import TableSampleMethod
from hare.query.queryset.constants import MAX_TABLE_SAMPLE_PERCENT, MAX_TABLE_SAMPLE_SEED


@dataclass(frozen=True, slots=True)
class TableSample:
    """The sample of the model's table ``sample()`` reads - ``TABLESAMPLE <method> (<percent>)
    [REPEATABLE (<seed>)]``.

    Args:
        method: How the rows are picked.
        percent: The chance of each row (``BERNOULLI``) or block (``SYSTEM``), in percent.
        seed: The seed repeating the same sample; None for a new one each run.
    """

    method: TableSampleMethod
    percent: float
    seed: int | None

    @classmethod
    def build(cls, percent: Any, method: Any, seed: Any) -> TableSample:
        """The sample of ``sample()``'s arguments.

        Args:
            percent: An int or float in ``0..MAX_TABLE_SAMPLE_PERCENT``.
            method: A ``TableSampleMethod`` or its name, in any case.
            seed: None, or an int in ``0..MAX_TABLE_SAMPLE_SEED``.

        Returns:
            The sample.

        Raises:
            QueryError: An argument is of another type or out of its range.
        """
        if not FiniteNumbers.is_finite_number(percent) or not 0 <= percent <= MAX_TABLE_SAMPLE_PERCENT:
            raise QueryError(f"sample() takes a percent from 0 to {MAX_TABLE_SAMPLE_PERCENT}, got {percent!r}")
        method_names = ", ".join(sample_method.value for sample_method in TableSampleMethod)
        if not isinstance(method, str) or method.upper() not in set(TableSampleMethod):
            raise QueryError(f"sample(method=...) takes one of {method_names}, got {method!r}")
        if seed is not None and (
            isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_TABLE_SAMPLE_SEED
        ):
            raise QueryError(f"sample(seed=...) takes None or an int from 0 to {MAX_TABLE_SAMPLE_SEED}, got {seed!r}")
        return cls(TableSampleMethod(method.upper()), percent, seed)
