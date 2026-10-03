"""The byte keys ``ORDER BY`` sorts SQLite decimal, time and JSON values by: the native ones are the
Python ones, and sorting by a key orders values exactly as the collation or comparison does."""

import functools
import json
import random

import pytest

from hare.dialects.sqlite.functions.collations.sqlite_decimal_collation import SqliteDecimalCollation
from hare.dialects.sqlite.functions.datetime.sqlite_time_collation import SqliteTimeCollation
from hare.dialects.sqlite.functions.json.sqlite_json_ordering import SqliteJsonOrdering
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions

DECIMAL_TEXTS = [
    "0", "-0", "0.00", "1", "1.0", "1.50", "-1.5", "15e-1", "123.45", "-123.45", "1e20", "-1e-20", ".5", "5.",
    "9999999999999999999999.99", "0.000001", "Infinity", "-inf", "NaN", "sNaN", " 1.5", "1.5 ", "1_000", "abc",
    "", "١٢", "1e", "--1", "+2",
]  # fmt: skip
TIME_TEXTS = [
    "00:00", "10:30", "10:30:05", "10:30:05.25", "10:30:05.123456", "10:30:00+03:00", "07:30:00+00:00",
    "10:30:00-02:30", "23:59:59.999999-12:00", "24:00", "10:30Z", "10:30:00+0300", "1030", "10:30:05.1234567",
    "10:00:00+05:30:15", "not a time", "",
]  # fmt: skip


def get_json_values() -> list:
    generator = random.Random(3)

    def make(depth: int):
        kind = generator.randint(0, 7 if depth < 3 else 4)
        if kind == 0:
            return None
        if kind == 1:
            return generator.choice(["", "a", "ab", "b", "é", "a\u0000b", "Z"])
        if kind == 2:
            return generator.choice([0, -1, 1, 2, 10**20, -(10**20)])
        if kind == 3:
            return generator.choice([0.5, -0.5, 1.0, 1e-7, 1e16, 3.25])
        if kind == 4:
            return generator.choice([True, False])
        if kind in (5, 6):
            return [make(depth + 1) for _ in range(generator.randint(0, 3))]
        return {
            generator.choice(["a", "b", "aa", "ab", "é", "zz"]): make(depth + 1)
            for _ in range(generator.randint(0, 3))
        }

    values = [make(0) for _ in range(400)] + [[], {}, None, [None], [[]], {"a": None}]
    return values


def get_json_inputs() -> list:
    inputs = []
    for value in get_json_values():
        inputs.append(json.dumps(value))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            inputs.append(value)
    return inputs + ["NaN", "[1, 2", "1e400", '"\\ud800"', 7, 2.5, 10**30]


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


def test_time_keys_order_as_the_collation() -> None:
    assert_keys_order_as_compare(SqliteTimeCollation.get_sort_key_bytes, SqliteTimeCollation.compare, TIME_TEXTS[:-8])


def test_json_keys_order_as_the_comparison() -> None:
    texts = [json.dumps(value) for value in get_json_values()]
    assert_keys_order_as_compare(SqliteJsonOrdering.get_sort_key_bytes, SqliteJsonOrdering.compare, texts[:150])


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

    def test_time(self) -> None:
        order = SqliteNativeFunctions.module.TimeOrder(
            SqliteTimeCollation.get_sort_key_bytes, SqliteTimeCollation.compare
        )
        assert_native_keys_match(order, SqliteTimeCollation.get_sort_key_bytes, [*TIME_TEXTS, None, 5, 1.5, b"t"])
        for left in TIME_TEXTS:
            for right in TIME_TEXTS:
                assert order.compare(left, right) == SqliteTimeCollation.compare(left, right), (left, right)

    def test_json(self) -> None:
        order = SqliteNativeFunctions.module.JsonOrder(
            SqliteJsonOrdering.get_sort_key_bytes, SqliteJsonOrdering.compare
        )
        inputs = [value for value in get_json_inputs() if value not in ("NaN", "[1, 2")]
        assert_native_keys_match(order, SqliteJsonOrdering.get_sort_key_bytes, [*inputs, None])
        sample = get_json_inputs()[:120]
        for left in sample:
            for right in sample:
                try:
                    expected = SqliteJsonOrdering.compare(left, right)
                except (ValueError, KeyError) as error:
                    with pytest.raises(type(error)):
                        order.compare(left, right)
                    continue
                assert order.compare(left, right) == expected, (left, right)
        assert order.compare(None, "1") is None
        assert order.compare("[1, 2", None) is None
