"""The native decimal overflow, decimal stored text and number text functions give what the Python
ones give - the same value, or the same exception - for every argument."""

import decimal
import math
import random
import struct
from collections.abc import Callable
from typing import Any

import pytest

from hare.dialects.sqlite.constants import SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT
from hare.dialects.sqlite.functions.decimal.sqlite_decimal_collation import SqliteDecimalCollation
from hare.dialects.sqlite.functions.decimal.sqlite_decimal_storage import SqliteDecimalStorage
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.fields.constants import DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION
from hare.sql.functions.text.number_text import NumberText

pytestmark = pytest.mark.skipif(SqliteNativeFunctions.module is None, reason="rust.native isn't built")


def get_outcome(function: Callable[..., Any], *arguments: Any) -> tuple[str, Any]:
    try:
        result = function(*arguments)
    except Exception as error:
        return "error", type(error)
    return "value", (type(result), result)


def assert_same(native_function: Callable[..., Any], python_function: Callable[..., Any], *arguments: Any) -> None:
    native_outcome = get_outcome(native_function, *arguments)
    python_outcome = get_outcome(python_function, *arguments)
    if native_outcome[0] == python_outcome[0] == "value":
        python_result = python_outcome[1][1]
        if isinstance(python_result, float) and math.isnan(python_result):
            assert math.isnan(native_outcome[1][1]), arguments
            return
    assert native_outcome == python_outcome, arguments


def get_values() -> list[Any]:
    generator = random.Random(11)
    floats = [struct.unpack("<d", generator.getrandbits(64).to_bytes(8, "little"))[0] for _ in range(300)]
    floats += [round(generator.uniform(-1e6, 1e6), generator.randint(0, 8)) for _ in range(600)]
    floats += [generator.uniform(-1, 1) for _ in range(300)]
    floats += [0.0, -0.0, 0.1, 0.5, 1.005, 2.675, 1e16, 1e-7, 5e-324, 1.7976931348623157e308, math.inf, -math.inf]
    floats.append(math.nan)
    texts = [
        "0", "-0", "0.00", "1.005", "1.015", "1.025", "-1.005", "2.5", "3.5", "-2.5", "0.0001", "-0.0049", "0.005",
        "99999.995", "123456789012345678901234567890", "1e5", "1E-30", "-1.5e+3", "12.", ".5", "+7", "1e100001",
        "1e-100001", "abc", " 1.5", "1.5 ", "NaN", "-Infinity", "inf", "1_000", "", "1e", "１２",
    ]  # fmt: skip
    integers = [0, 1, -1, 7, 12345, -987654321, 2**63 - 1, -(2**63), 2**64, 10**40]
    return [*floats, *texts, *integers, None, b"1.5", b""]


LIMITS = [(5, 2), (10, 0), (28, 10), (40, 20), (3, 3), (1, 0), (50, 2), (2, -1), (0, 0), (30, 29)]


def test_decimal_overflow() -> None:
    overflows = SqliteNativeFunctions.module.DecimalOverflow(SqliteDecimalCollation.overflows).overflows
    for value in get_values():
        for max_digits, decimal_places in LIMITS:
            assert_same(overflows, SqliteDecimalCollation.overflows, value, max_digits, decimal_places)
    assert_same(overflows, SqliteDecimalCollation.overflows, "1.5", "5", 2)


def test_decimal_stored_text() -> None:
    get_stored_text = SqliteNativeFunctions.module.DecimalStoredText(
        SqliteDecimalStorage.get_stored_text,
        DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION,
        SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT,
    ).get_stored_text
    for value in get_values():
        for max_digits, decimal_places in LIMITS:
            assert_same(get_stored_text, SqliteDecimalStorage.get_stored_text, value, max_digits, decimal_places)
    for decimal_places in [999, 1000, 1001, 1200]:
        assert_same(get_stored_text, SqliteDecimalStorage.get_stored_text, "1.5", 2000, decimal_places)


def refuse(*arguments: Any) -> Any:
    raise AssertionError(f"the Python function was called with {arguments}")


def test_stored_values_never_reach_python() -> None:
    module = SqliteNativeFunctions.module
    overflows = module.DecimalOverflow(refuse).overflows
    get_stored_text = module.DecimalStoredText(refuse, 28, SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT).get_stored_text
    format_number = module.NumberText(refuse, 28).format_number
    for value in ["12.50", "-0.001", 3, 2.675, None, b"x"]:
        overflows(value, 5, 2)
        get_stored_text(value, 5, 2)
        if not isinstance(value, bytes):
            format_number(value, 2)
            format_number(value if not isinstance(value, str) else float(value), None)
    assert get_stored_text(2.675, 5, 2) == "2.67"
    assert format_number(2.675, 2) == "2.68"
    assert format_number(1e15, None) == "1e+15"


def test_number_text() -> None:
    format_number = SqliteNativeFunctions.module.NumberText(
        NumberText.format_number, decimal.DefaultContext.prec
    ).format_number
    for value in get_values():
        for scale in [None, 0, 1, 2, 5, 27, 30, -1, "x"]:
            assert_same(format_number, NumberText.format_number, value, scale)
