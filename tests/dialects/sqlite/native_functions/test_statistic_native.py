"""The native variance and standard deviation aggregates give the statistic of the rows in their
frame - as plain aggregates and as window functions, over ints, floats, numbers as text and NULLs."""

import random
from functools import partial

import pytest

from hare.dialects.sqlite.functions.sqlite_math_functions import SqliteMathFunctions
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.functions.statistics.sqlite_statistic import SqliteStatistic
from hare.dialects.sqlite.functions.statistics.sqlite_statistics import SqliteStatistics

pytestmark = [
    pytest.mark.database_independent,
]


def get_series() -> list[list]:
    generator = random.Random(9)
    return [
        [],
        [None],
        [5],
        [1, 2, 3, 4],
        [1, None, 3],
        [2**40, -(2**40), 7],
        [2**62, 2**62, 1],
        [generator.randint(-1000, 1000) for _ in range(200)],
        [0.5, 1.25, -3.0],
        [generator.random() for _ in range(50)],
        ["1.50", "2.25", "3"],
        [1, "2.5", 3.5],
        [b"1.5", 2],
    ]


def run(aggregate_factory, values: list, window: int | None):
    aggregate = aggregate_factory()
    results = []
    for index, value in enumerate(values):
        aggregate.step(value)
        if window is not None and index >= window:
            aggregate.inverse(values[index - window])
        results.append(aggregate.value())
    return results, aggregate.finalize()


def get_expected(statistic_class, values: list, window: int | None):
    """The statistic of the frame after every row - the last ``window`` rows, NULLs skipped - and
    of the whole frame at the end."""
    results = []
    for index in range(len(values)):
        start = 0 if window is None else max(0, index - window + 1)
        numbers = [SqliteMathFunctions.get_number(value) for value in values[start : index + 1] if value is not None]
        results.append(SqliteStatistic.get_statistic(numbers, statistic_class.sample, statistic_class.is_deviation))
    if not results:
        return results, SqliteStatistic.get_statistic([], statistic_class.sample, statistic_class.is_deviation)
    return results, results[-1]


@pytest.mark.parametrize("statistic_class", list(SqliteStatistics.CLASSES.values()))
def test_statistics(statistic_class) -> None:
    native = partial(
        SqliteNativeFunctions.module.Statistic,
        statistic_class.sample,
        statistic_class.is_deviation,
        SqliteStatistic.get_statistic,
    )
    for values in get_series():
        for window in (None, 2, 3):
            assert run(native, values, window) == get_expected(statistic_class, values, window), (values, window)
