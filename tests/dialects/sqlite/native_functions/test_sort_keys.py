"""The byte keys ``ORDER BY`` sorts SQLite decimal values by: the native ones are the Python ones, and
sorting by a key orders values exactly as the collation does."""

import functools

import pytest

from hare.dialects.sqlite.functions.decimal.sqlite_decimal_collation import SqliteDecimalCollation
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions

pytestmark = pytest.mark.database_independent

DECIMAL_TEXTS = [
    "0", "-0", "0.00", "1", "1.0", "1.50", "-1.5", "15e-1", "123.45", "-123.45", "1e20", "-1e-20", ".5", "5.",
    "9999999999999999999999.99", "0.000001", "Infinity", "-inf", "NaN", "sNaN", " 1.5", "1.5 ", "1_000", "abc",
    "", "١٢", "1e", "--1", "+2",
]  # fmt: skip


def assert_native_keys_match(order, python_key, inputs) -> None:
    for value in inputs:
        assert order.sort_key(value) == python_key(value), value


def assert_keys_order_as_compare(python_key, compare, texts) -> None:
    by_compare = sorted(texts, key=functools.cmp_to_key(compare))
    by_key = sorted(texts, key=python_key)
    assert [python_key(text) for text in by_compare] == [python_key(text) for text in by_key]
    for left in texts:
        for right in texts:
            expected = compare(left, right)
            got = (python_key(left) > python_key(right)) - (python_key(left) < python_key(right))
            assert got == expected, (left, right)


def test_decimal_keys_order_as_the_collation() -> None:
    assert_keys_order_as_compare(
        SqliteDecimalCollation.get_sort_key_bytes, SqliteDecimalCollation.compare, DECIMAL_TEXTS
    )


@pytest.mark.skipif(SqliteNativeFunctions.module is None, reason="rust.native isn't built")
class TestNativeKeys:
    def test_decimal(self) -> None:
        order = SqliteNativeFunctions.module.DecimalOrder(
            SqliteDecimalCollation.get_sort_key_bytes, SqliteDecimalCollation.compare
        )
        inputs = [*DECIMAL_TEXTS, None, 0, -3, 2**70, 1.5, -0.0, 1e300, float("inf"), b"\x00\x01", b""]
        assert_native_keys_match(order, SqliteDecimalCollation.get_sort_key_bytes, inputs)
        for left in DECIMAL_TEXTS:
            for right in DECIMAL_TEXTS:
                assert order.compare(left, right) == SqliteDecimalCollation.compare(left, right), (left, right)
        for value in [None, 1, 2.5, "3.10", b"x"]:
            assert order.get_decimal_text(value) == SqliteDecimalCollation.get_decimal_text(value)
