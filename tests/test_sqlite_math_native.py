"""The native math functions give what the Python ones give - and raise what they raise - for every
argument of a grid: ints, floats, decimal text, zeros, infinities, values outside a domain."""

import itertools
import math

import pytest

from hare.dialects.sqlite.functions.sqlite_math_functions import SqliteMathFunctions
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions

pytestmark = pytest.mark.skipif(SqliteNativeFunctions.module is None, reason="rust.native isn't built")

ARGUMENTS = [
    None, 0, 1, -1, 2, 7, -7, 2**62, -(2**63), 0.0, -0.0, 0.5, -0.5, 1.5, -2.5, 1e-300, 1e300, 709.0, 710.0,
    math.pi / 2, -1e308, math.inf, -math.inf, "0", "-0.00", "1.5", "-1.5", "2.000", "100", "-3.7", "1e2",
    "12345678901234567890.5", "abc", " 1", b"2",
]  # fmt: skip


def call_both(native_call, python_call, arguments):
    try:
        expected = python_call(*arguments)
    except Exception as error:  # noqa: BLE001 - the error is what is compared.
        with pytest.raises(type(error)):
            native_call(*arguments)
        return
    result = native_call(*arguments)
    if isinstance(expected, float) and math.isnan(expected):
        assert isinstance(result, float) and math.isnan(result), arguments
        return
    assert result == expected, arguments
    assert type(result) is type(expected), arguments
    if isinstance(expected, float):
        assert math.copysign(1, result) == math.copysign(1, expected), arguments


def test_math_functions() -> None:
    functions = SqliteMathFunctions.get_functions()
    native = SqliteNativeFunctions.module.MathFunctions(functions)
    for name, (argument_count, python_function) in functions.items():
        native_function = getattr(native, name)
        for arguments in itertools.product(ARGUMENTS, repeat=argument_count):
            call_both(native_function, python_function, arguments)
