"""The native canonical JSON text is the Python one for every value - floats written as repr() writes
them, integral floats as exact ints, keys sorted, strings escaped as json.dumps(ensure_ascii=False)."""

import json
import math
import random
import struct

import pytest

from hare.dialects.sqlite.functions.json.sqlite_json_equality import SqliteJsonEquality
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions

pytestmark = pytest.mark.skipif(SqliteNativeFunctions.module is None, reason="rust.native isn't built")


@pytest.fixture
def canonicalize():
    return SqliteNativeFunctions.module.JsonCanonical(SqliteJsonEquality.canonicalize).canonicalize


def get_random_floats(count: int) -> list[float]:
    generator = random.Random(4)
    floats: list[float] = []
    while len(floats) < count:
        number = struct.unpack("<d", generator.getrandbits(64).to_bytes(8, "little"))[0]
        if math.isfinite(number):
            floats.append(number)
    floats += [generator.uniform(-1e-3, 1e-3) for _ in range(count)]
    floats += [round(generator.uniform(-1e6, 1e6), generator.randint(0, 8)) for _ in range(count)]
    return floats + [1e-05, 0.0001, 1.5e-07, 1e16, 1e17, 2.0**127, 1.7e38, 1e300, -0.0, 0.1, 123456789012345678.0]


def test_floats_and_their_texts(canonicalize) -> None:
    floats = get_random_floats(20_000)
    assert canonicalize(json.dumps(floats)) == SqliteJsonEquality.canonicalize(json.dumps(floats))
    for number in floats[:2000]:
        assert canonicalize(number) == SqliteJsonEquality.canonicalize(number), number
        assert canonicalize(repr(number)) == SqliteJsonEquality.canonicalize(repr(number)), number


def test_values(canonicalize) -> None:
    texts = [
        '{"b": 1, "a": [1.0, 2.5, {"z": null, "é": "x\\u0001\\"\\\\\\n\\t\\u007f"}], "a1": true}',
        '{"a": 1, "a": 2}', "[]", "{}", '"text"', "-0", "1E400", "1e5", "12345678901234567890123456789012345678901",
        "NaN", "[1, 2", "not json", '"\\ud800"', '{"k": -1.0e-10}', " [ 1 , 2 ] ",
    ]  # fmt: skip
    for text in texts:
        assert canonicalize(text) == SqliteJsonEquality.canonicalize(text), text
        assert canonicalize(text.encode()) == SqliteJsonEquality.canonicalize(text.encode()), text
    for value in [None, 0, -5, 2**70, 1.0, 2.5, float("inf")]:
        assert canonicalize(value) == SqliteJsonEquality.canonicalize(value), value
