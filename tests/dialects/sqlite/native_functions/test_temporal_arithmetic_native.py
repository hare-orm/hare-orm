"""The native date and datetime shift and difference give what the Python functions give - and raise
what they raise - for aware, naive and boundary values."""

import pytest

from hare.dialects.sqlite.functions.datetime.temporal_arithmetic_functions import TemporalArithmeticFunctions
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from tests.dialects.sqlite.native_functions.function_comparison import FunctionComparison

pytestmark = [
    pytest.mark.database_independent,
]

DATETIMES = [
    "2024-03-01 10:00:00+00:00", "2024-03-01 10:00:00.250000+03:00", "2024-02-29 23:59:59.999999-05:30",
    "0001-01-01 00:30:00+01:00", "9999-12-31 23:30:00+01:00", "9999-12-31 23:00:00-02:00", "2024-03-01 10:00:00",
    "2024-03-01T10:00", "2024-03-01", "2024-03-01 10:00:00Z", "bad", None,
]  # fmt: skip
SHIFTS = [0, 1, -1, 86_400_000_000, -86_400_000_001, 3_600_000_000, 10**18, None]


def test_shift_and_difference() -> None:
    native = SqliteNativeFunctions.module.TemporalArithmetic(TemporalArithmeticFunctions)
    for value in DATETIMES:
        for microseconds in SHIFTS:
            for sign in (1, -1):
                FunctionComparison.assert_equal(
                    native.shift_datetime, TemporalArithmeticFunctions.shift_datetime, value, microseconds, sign
                )
                FunctionComparison.assert_equal(
                    native.shift_date, TemporalArithmeticFunctions.shift_date, value, microseconds, sign
                )
        for other in DATETIMES:
            FunctionComparison.assert_equal(
                native.difference_datetime, TemporalArithmeticFunctions.difference_datetime, value, other
            )
            FunctionComparison.assert_equal(
                native.difference_date, TemporalArithmeticFunctions.difference_date, value, other
            )
