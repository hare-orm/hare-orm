"""The native variance and standard deviation aggregates give what the Python ones give - as plain
aggregates and as window functions, over ints, floats, decimal text and NULLs."""

import random
from decimal import Decimal
from functools import partial

import pytest

from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.functions.statistics.sqlite_statistic import SqliteStatistic
from hare.dialects.sqlite.functions.statistics.sqlite_statistics import SqliteStatistics

pytestmark = pytest.mark.skipif(SqliteNativeFunctions.module is None, reason="rust.native isn't built")


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


@pytest.mark.parametrize("statistic_class", list(SqliteStatistics.CLASSES.values()))
def test_statistics(statistic_class) -> None:
    native = partial(
        SqliteNativeFunctions.module.Statistic,
        statistic_class.sample,
        statistic_class.is_deviation,
        SqliteStatistic.get_statistic,
        Decimal,
    )
    for values in get_series():
        for window in (None, 2, 3):
            assert run(native, values, window) == run(statistic_class, values, window), (values, window)
