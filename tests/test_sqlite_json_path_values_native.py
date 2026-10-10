"""The native JSON path, JSON value and JSON date functions give what the Python ones give - the same
value, or the same exception - for every argument."""

import datetime
import json
import math
import random
import struct
from collections.abc import Callable
from typing import Any

import pytest

from hare.dialects.sqlite.functions.json.sqlite_json_datetime import SqliteJsonDatetime
from hare.dialects.sqlite.functions.json.sqlite_json_path import SqliteJsonPath
from hare.dialects.sqlite.functions.json.sqlite_json_values import SqliteJsonValues
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions

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
    if native_outcome[0] == "value" and python_outcome[0] == "value":
        native_result, python_result = native_outcome[1][1], python_outcome[1][1]
        if isinstance(python_result, float) and math.isnan(python_result):
            assert isinstance(native_result, float) and math.isnan(native_result), arguments
            return
    assert native_outcome == python_outcome, arguments


DOCUMENTS = [
    '{"a": 1, "b": [10, 20.5, {"c": "x"}], "1": "one", "n": null, "t": true, "f": false}',
    '{"a": {"b": {"c": [1, 2, 3]}}, "s": "é\\u0001\\"\\\\", "big": 123456789012345678901234567890}',
    '[1, "two", [3, [4]], {"5": 5}, 1e400, -0, 1E2, 0.1, 9223372036854775807, -9223372036854775809]',
    '{"a": 1, "a": 2}', '"text"', "12", "1.5", "null", "true", "[]", "{}", " [ 1 , 2 ] ",
    "NaN", "not json", '{"k": NaN}', "[1, 2",
]  # fmt: skip
PATHS = [
    "[]", '["a"]', '["b", 1]', '["b", 2, "c"]', '["b", -1]', '["b", -4]', '["b", 3]', '[1]', '["1"]', "[0]",
    "[2, 1, 0]", "[3, 5]", '[3, "5"]', "[4]", "[5]", "[6]", "[7]", "[8]", "[9]", '["a", "b", "c", 2]',
    '["big"]', '["s"]', '["n"]', '["t"]', '["f"]', '["missing"]', "[true]", "[1.0]", "[null]",
    "[18446744073709551616]", '{"a": 1}', "not json",
]  # fmt: skip


def test_json_path() -> None:
    extract = SqliteNativeFunctions.module.JsonPath(SqliteJsonPath.extract).extract
    for document in [*DOCUMENTS, *(text.encode() for text in DOCUMENTS), None, 5, -3, 2.5, 1e16, 1e-05]:
        for path in PATHS:
            for mode in ["text", "json", "type", "other"]:
                assert_same(extract, SqliteJsonPath.extract, document, path, mode)


def get_random_floats(count: int) -> list[float]:
    generator = random.Random(7)
    floats: list[float] = []
    while len(floats) < count:
        floats.append(struct.unpack("<d", generator.getrandbits(64).to_bytes(8, "little"))[0])
    floats += [generator.uniform(-1e-3, 1e-3) for _ in range(count)]
    floats += [round(generator.uniform(-1e6, 1e6), generator.randint(0, 8)) for _ in range(count)]
    return floats + [1e-05, 1.5e-07, 1e16, 1e17, 2.0**127, 1e300, -0.0, 0.0, 0.1, 123.0, 5e-324, math.nan, math.inf]


def test_json_float() -> None:
    functions = SqliteNativeFunctions.module.JsonValues(SqliteJsonValues)
    for value in get_random_floats(5000):
        assert_same(functions.format_float, SqliteJsonValues.format_float, value)
    for value in [None, 0, 7, -12, 2**53, 2**53 + 1, -(2**53) - 1, 2**70, 10**20, "1.5", b"1"]:
        assert_same(functions.format_float, SqliteJsonValues.format_float, value)


def get_moment_texts() -> list[str]:
    generator = random.Random(3)
    texts = []
    for _ in range(3000):
        moment = datetime.datetime(2000, 1, 1) + datetime.timedelta(
            days=generator.randint(-700_000, 2_900_000), microseconds=generator.randint(0, 86_400_000_000 - 1)
        )
        fraction = generator.choice([0, 1, 3, 6])
        text = moment.isoformat(sep=generator.choice(["T", " "]), timespec="microseconds")
        text = text[: 20 + fraction] if fraction else text[:19]
        texts.append(text + generator.choice(["", "", "+00:00", "-05:30", "+14:00", "-00:00", "Z", "+0530"]))
    return texts + [
        "2020-01-02", "2020-01-02T03:04", "2020-01-02 03:04:05.1234567", "2020-02-30T00:00:00",
        "0001-01-01T00:00:00+01:00", "0001-01-01T00:30:00", "0999-12-31T23:00:00", "9999-12-31T23:59:59-01:00",
        "9999-12-31T23:59:59.999999", "2020-01-02T24:00:00", "2020-01-02T10:60:00", "20200102", "2020-W01-1",
        "2020-01-02T03", "not a date", "", "2020-01-02T03:04:05+24:00",
    ]  # fmt: skip


def test_json_timestamp() -> None:
    functions = SqliteNativeFunctions.module.JsonValues(SqliteJsonValues)
    for text in get_moment_texts():
        for is_aware in [0, 1]:
            assert_same(functions.format_timestamp, SqliteJsonValues.format_timestamp, text, is_aware)
    for value in [None, 5, b"2020-01-02"]:
        assert_same(functions.format_timestamp, SqliteJsonValues.format_timestamp, value, 1)


def test_json_time() -> None:
    functions = SqliteNativeFunctions.module.JsonValues(SqliteJsonValues)
    generator = random.Random(5)
    texts = []
    for _ in range(3000):
        clock = datetime.time(
            generator.randint(0, 23), generator.randint(0, 59), generator.randint(0, 59), generator.randint(0, 999_999)
        )
        text = clock.isoformat(timespec=generator.choice(["minutes", "seconds", "milliseconds", "microseconds"]))
        texts.append(text + generator.choice(["", "+00:00", "-00:00", "+05:30", "-02:00", "-23:59", "Z", "+0300"]))
    texts += ["10:30:00.5", "10:30:00.1234567", "24:00", "10", "1030", "10:30:00+03:00:30", "x", ""]
    for text in [*texts, None, 5, b"10:30"]:
        assert_same(functions.format_time, SqliteJsonValues.format_time, text)


def test_json_datetime() -> None:
    normalize = SqliteNativeFunctions.module.JsonDatetime(SqliteJsonDatetime).normalize
    texts = [
        *get_moment_texts(),
        "2020-01-02T03:04:05.0000005",
        "2020-01-02T03:04:05.0000015",
        "2020-01-02T03:04:05.00000051",
        "9999-12-31T23:59:59.9999999",
        "9999-12-31T23:59:59.9999995",
        "2020-01-02T03:04-05",
        "2020-01-02T03:04+0530",
        "2020-01-02T03:04:05.+01",
        "2020-01-02\n",
        "2020-01-02T03:04:05Z\n",
        "0000-01-01",
        "2020-1-02",
        "２０２０-01-02",
    ]
    modes = ["wall", "instant", "YEAR", "ISOYEAR", "QUARTER", "MONTH", "WEEK", "DOW", "ISODOW", "DAY", "HOUR"]
    modes += ["MINUTE", "SECOND", "MICROSECOND", "EPOCH", "other"]
    for text in texts:
        for mode in modes:
            assert_same(normalize, SqliteJsonDatetime.normalize, text, mode)
    for value in [None, 5, 2.5, b"2020-01-02"]:
        assert_same(normalize, SqliteJsonDatetime.normalize, value, "wall")
    assert_same(normalize, SqliteJsonDatetime.normalize, "2020-01-02", None)
    assert json.dumps(normalize("2020-01-02T03:04:05+01:00", "instant")) == "1577930645000000"
