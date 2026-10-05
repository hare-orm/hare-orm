"""The native JSON text is orjson's text for every plain JSON value, and nothing for a value a JSON
column can't store or orjson doesn't encode."""

import math
import random
import re
import struct

import pytest

from hare.fields.data.json import JsonCodec

orjson = pytest.importorskip("orjson")
pytestmark = pytest.mark.skipif(JsonCodec.native_encoder is None, reason="rust.native isn't built")


def get_random_floats(count: int) -> list[float]:
    generator = random.Random(20261003)
    floats: list[float] = []
    while len(floats) < count:
        number = struct.unpack("<d", generator.getrandbits(64).to_bytes(8, "little"))[0]
        if math.isfinite(number):
            floats.append(number)
    floats += [round(generator.uniform(-1e6, 1e6), generator.randint(0, 8)) for _ in range(count)]
    floats += [0.0, -0.0, 5e-324, 1.7976931348623157e308, 1e16, 1e15, 1e-5, 1e-6, 153838026194641.12]
    return floats


def test_floats_are_written_as_orjson_writes_them() -> None:
    floats = get_random_floats(100_000)
    # orjson before 3.12 writes a positive exponent without its sign (1e202) - the same number.
    orjson_text = re.sub(r"e(\d)", r"e+\1", orjson.dumps(floats).decode())
    assert JsonCodec.native_encoder(floats) == orjson_text


def test_strings_ints_and_nesting_are_written_as_orjson_writes_them() -> None:
    generator = random.Random(7)
    strings = ["".join(chr(generator.randint(1, 0x2FF)) for _ in range(generator.randint(0, 12))) for _ in range(5000)]
    strings += ['\x01\x1f"\\/\x7f \U0001f600', ""]
    value = {
        "strings": strings,
        "ints": [0, -1, 2**63 - 1, -(2**63), 2**64 - 1],
        "nested": [{"a": (1, [None, True, False])}, {}, []],
        "é": {"key": "value"},
    }
    assert JsonCodec.native_encoder(value) == orjson.dumps(value).decode()


@pytest.mark.parametrize(
    "value",
    [float("nan"), [float("inf")], {"a": "x\x00y"}, {"x\x00": 1}, 2**64, -(2**63) - 1, {1: "a"}, {"a": {1, 2}}],
)
def test_a_value_it_cant_write_gives_nothing(value) -> None:
    assert JsonCodec.native_encoder(value) is None


def test_deep_nesting_gives_nothing() -> None:
    value: list = []
    for _ in range(300):
        value = [value]
    assert JsonCodec.native_encoder(value) is None
